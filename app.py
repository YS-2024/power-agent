from __future__ import annotations

import argparse
import json
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from power_agent.agent import PowerAgent, load_env
from power_agent.knowledge import KnowledgeBase
from power_agent.storage import create_knowledge_store
from power_agent.postgres import StorageError
from power_agent.embeddings import EmbeddingError
from power_agent.document_import import parse_upload
from power_agent.assistant import ConversationalAssistant
from power_agent.accounts import WorkspaceManager

ROOT = Path(__file__).resolve().parent


def create_server(port=8000, data_dir=None, seed_dir=None, knowledge_base=None, access=None):
    data_dir = Path(data_dir) if data_dir else ROOT / "data"
    kb = knowledge_base if knowledge_base is not None else create_knowledge_store(data_dir)
    marker = data_dir / ".initialized"
    initialized = kb.initialized if hasattr(kb, "initialized") else marker.exists()
    if not initialized:
        if hasattr(kb, "embeddings") and (data_dir / "knowledge.db").exists():
            raise ValueError("发现已有 SQLite 资料，请先运行 migrate_postgres.py 完成迁移，再启动应用。")
        for path in sorted((Path(seed_dir) if seed_dir else ROOT / "knowledge").glob("*.md")):
            content = path.read_text(encoding="utf-8")
            source = re.search(r"^参考来源：(https://\S+)", content, re.M)
            kb.add(path.name, content, source_url=source.group(1) if source else "", kind="内置学习资料")
        if hasattr(kb, "mark_initialized"):
            kb.mark_initialized()
        else:
            marker.touch()
    agent = PowerAgent(kb, data_dir / "reports")
    assistant = ConversationalAssistant(agent)
    csrf_token = secrets.token_urlsafe(32)
    accounts = getattr(access, 'accounts', None)
    workspaces = WorkspaceManager(kb, data_dir) if accounts else None

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Request bodies, source text and API credentials are never logged.
            super().log_message(fmt, *args)

        def send(self, status, body, content_type="application/json; charset=utf-8", download=False, headers=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="power-knowledge-report.md"')
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # The browser can disconnect after the stop endpoint confirms cancellation.
                self.close_connection = True

        def check_local(self):
            if access is not None:
                try:
                    return access.check(self, csrf_token)
                except StorageError as exc:
                    self.send(503, {"error": str(exc)})
                    return False
            allowed = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            if self.headers.get("Host") not in allowed:
                self.send(403, {"error": "仅允许通过本机地址访问。"})
                return False
            origin = self.headers.get("Origin")
            if origin and origin not in {"http://" + host for host in allowed}:
                self.send(403, {"error": "不允许跨站请求。"})
                return False
            if self.command in {"POST", "DELETE"} and self.headers.get("X-CSRF-Token") != csrf_token:
                self.send(403, {"error": "会话已失效，请刷新页面。"})
                return False
            return True

        def workspace(self):
            return workspaces.get(self.user) if workspaces else (kb, agent, assistant)

        def require_admin(self):
            if not accounts or self.user['role'] != 'admin':
                raise PermissionError('仅管理员可以管理账号。')

        def do_GET(self):
            try:
                self.handle_get()
            except StorageError as exc:
                self.send(503, {"error": str(exc)})
            except KeyError:
                self.send(404, {"error": "会话或记忆不存在。"})
            except ValueError:
                self.send(400, {"error": "请求参数不正确。"})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})

        def handle_get(self):
            if not self.check_local():
                return
            kb, agent, assistant = self.workspace()
            path = urlsplit(self.path).path
            if path == '/api/users':
                self.require_admin()
                return self.send(200, accounts.list())
            if path == "/api/conversations":
                return self.send(200, assistant.store.list_sessions())
            if path == "/api/memories":
                return self.send(200, assistant.store.memories())
            if re.fullmatch(r"/api/conversations/[a-f0-9]{32}", path):
                before = parse_qs(urlsplit(self.path).query).get("before", [None])[0]
                return self.send(200, assistant.store.session(path.rsplit("/", 1)[1], before))
            if path == "/api/status":
                docs = kb.list_documents()
                return self.send(200, {"shared_demo": access is not None, "user": getattr(self, 'user', None), "engine": "LangChain + LangGraph", "database": kb.backend, "retrieval": "hybrid" if hasattr(kb, "embeddings") else "lexical", "embedding_model": getattr(getattr(kb, "embeddings", None), "model", ""), "mode": "model" if agent.model_enabled else "extractive", "model": agent.model if agent.model_enabled else "", "provider": agent.provider if agent.model_enabled else "", "documents": len(docs), "chunks": kb.chunk_count, "csrf_token": getattr(self, 'session_csrf', csrf_token)})
            if path == "/api/documents":
                return self.send(200, kb.list_documents())
            if re.fullmatch(r"/api/documents/[a-f0-9]{32}", path):
                try:
                    return self.send(200, kb.get(path.rsplit("/", 1)[1]))
                except KeyError:
                    return self.send(404, {"error": "资料不存在。"})
            if re.fullmatch(r"/api/reports/[a-f0-9]{32}", path):
                file = agent.report_dir / (path.rsplit("/", 1)[1] + ".md")
                if file.is_file():
                    return self.send(200, file.read_bytes(), "text/markdown; charset=utf-8", download=True)
                return self.send(404, {"error": "报告不存在。"})
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/chat-utils.js": ("chat-utils.js", "text/javascript"), "/styles.css": ("styles.css", "text/css")}
            if path in files:
                filename, mime = files[path]
                return self.send(200, (ROOT / "static" / filename).read_bytes(), mime + "; charset=utf-8")
            self.send(404, {"error": "页面不存在。"})

        def do_POST(self):
            if not self.check_local():
                return
            try:
                kb, agent, assistant = self.workspace()
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("请求必须使用 application/json。")
                length = int(self.headers.get("Content-Length", "0"))
                path = urlsplit(self.path).path
                limit = 17_000_000 if path in {"/api/daily-chat", "/api/documents", "/api/assistant"} else 2_000_000
                if length <= 0 or length > limit:
                    self.close_connection = True
                    return self.send(413, {"error": "请求为空或超过大小限制，请减少附件。"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise ValueError("请求必须为 JSON 对象。")
                path = urlsplit(self.path).path
                if path == '/api/users':
                    self.require_admin()
                    return self.send(201, accounts.create(body.get('username'), body.get('password'), body.get('role','member'), body.get('can_read_public',True)))
                if re.fullmatch(r'/api/users/[a-f0-9]{32}', path):
                    self.require_admin()
                    return self.send(200, accounts.update(self.user, path.rsplit('/',1)[1], active=body.get('active'),
                        can_read_public=body.get('can_read_public'), password=body.get('password')))
                if path == '/api/auth/password' and accounts:
                    accounts.change_password(self.user, body.get('current_password'), body.get('new_password'))
                    return self.send(200, {'ok':True})
                if path == "/api/conversations":
                    return self.send(201, assistant.store.create())
                if path == "/api/memories":
                    return self.send(200, assistant.store.save_memory(body.get("id"), body.get("content", "")))
                if path == "/api/assistant":
                    result = assistant.run(body.get("message", ""), body.get("session_id"), body.get("request_id"),
                                           images=body.get("images", []), remember=body.get("remember", True))
                    return self.send(200 if result["ok"] else 503, result)
                if path == "/api/assistant/cancel":
                    return self.send(200, assistant.cancel(body.get("session_id"), body.get("request_id")))
                if path == "/api/documents":
                    destination = kb.for_import(body.get('scope', 'private')) if workspaces else kb
                    if "data_base64" in body:
                        filename = body.get("filename", "")
                        parsed = parse_upload(filename, body["data_base64"])
                        result = destination.add(filename, parsed["content"], parsed=True,
                                        title=Path(filename.replace("\\", "/")).stem)
                        return self.send(201, dict(result, warnings=parsed["warnings"]))
                    return self.send(201, destination.add(body.get("filename", ""), body.get("content", "")))
                if path == "/api/daily-chat":
                    result = agent.daily_chat(body.get("message", ""), body.get("history", []), body.get("images", []))
                    return self.send(200 if result["ok"] else 503, result)
                question = body.get("question", "")
                if not isinstance(question, str) or not 2 <= len(question.strip()) <= 2000:
                    raise ValueError("请输入 2～2000 字的问题或主题。")
                if path == "/api/search":
                    results = kb.search(question, limit=8)
                    return self.send(200, {"sources": results, "notice": getattr(results, "notice", ""), "retrieval": getattr(results, "mode", "lexical")})
                if path in {"/api/chat", "/api/report"}:
                    return self.send(200, agent.run(question, report=path == "/api/report", previous_question=body.get("previous_question", "")))
                self.send(404, {"error": "接口不存在。"})
            except (StorageError, EmbeddingError) as exc:
                self.send(503, {"error": str(exc)})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except KeyError:
                self.send(404, {"error": "会话或记忆不存在。"})
            except (ValueError, UnicodeError) as exc:
                self.send(400, {"error": str(exc) if not isinstance(exc, json.JSONDecodeError) else "JSON 格式不正确。"})
            except Exception:
                self.send(500, {"error": "本地服务处理失败，请检查资料目录是否可写。"})

        def do_DELETE(self):
            if not self.check_local():
                return
            try:
                kb, agent, assistant = self.workspace()
            except StorageError as exc:
                return self.send(503, {'error':str(exc)})
            path = urlsplit(self.path).path
            if path == "/api/memories" or re.fullmatch(r"/api/(conversations|memories)/[a-f0-9]{32}", path):
                try:
                    if path.startswith("/api/conversations/"):
                        assistant.store.delete_session(path.rsplit("/", 1)[1])
                    else:
                        assistant.store.delete_memory(None if path == "/api/memories" else path.rsplit("/", 1)[1])
                    return self.send(200, {"ok": True})
                except StorageError as exc:
                    return self.send(503, {"error": str(exc)})
                except KeyError:
                    return self.send(404, {"error": "会话不存在。"})
            if not re.fullmatch(r"/api/documents/[a-f0-9]{32}", path):
                return self.send(404, {"error": "接口不存在。"})
            try:
                kb.delete(path.rsplit("/", 1)[1])
                self.send(200, {"ok": True})
            except StorageError as exc:
                self.send(503, {"error": str(exc)})
            except PermissionError as exc:
                self.send(403, {"error": str(exc)})
            except KeyError:
                self.send(404, {"error": "资料不存在。"})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def main():
    parser = argparse.ArgumentParser(description="启动电力知识助手")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    load_env(ROOT / ".env")
    try:
        server = create_server(args.port)
    except (ValueError, StorageError, EmbeddingError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"启动失败：{exc}。端口被占用时可尝试 python app.py --port 8001", file=sys.stderr)
        return 1
    print(f"电力知识助手已启动：http://127.0.0.1:{server.server_port}（Ctrl+C 停止）", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
