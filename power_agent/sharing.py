"""Password-protected temporary demo, separate from the personal workspace."""
import hashlib
import json
import re
import secrets
import threading
import time
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from urllib.parse import urlsplit
from psycopg import sql
from .postgres import PostgresKnowledgeBase


def snapshot_knowledge(source, schema):
    if not re.fullmatch(r"demo_[a-f0-9]{32}", schema) or schema == source.schema:
        raise ValueError("演示空间必须使用独立的随机 schema。")
    target = PostgresKnowledgeBase(source.dsn, source.embeddings, schema=schema,
                                  min_similarity=source.min_similarity)
    with target.connect() as db:
        db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        if db.execute("SELECT 1 FROM documents LIMIT 1").fetchone() or db.execute("SELECT 1 FROM settings WHERE key='initialized'").fetchone():
            raise ValueError("演示空间已存在，请新建分享；不会覆盖现有数据。")
        for table, columns in [
            ("documents", "id,title,filename,content,digest,source_url,kind,created_at"),
            ("chunks", "id,document_id,ordinal,text,start_line,end_line,terms,embedding,location"),
        ]:
            db.execute(sql.SQL("INSERT INTO {} ({}) SELECT {} FROM {}.{}").format(
                sql.Identifier(table), sql.SQL(columns), sql.SQL(columns),
                sql.Identifier(source.schema), sql.Identifier(table)))
        db.execute("INSERT INTO settings VALUES ('initialized','1')")
    return target


class ShareAccess:
    def __init__(self, password=None, accounts=None):
        if accounts is None and (not isinstance(password, str) or len(password) < 24):
            raise ValueError("分享密码至少 24 字符。")
        self.accounts = accounts
        self.expected = ("guest:" + password).encode() if password else b''
        self.public_host = ""
        self.sessions = {}
        self.failures = []
        self.lock = threading.Lock()

    def cookie(self, token, secure, max_age=28800):
        return f"power_demo_session={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}" + ("; Secure" if secure else "")

    def session_key(self, handler):
        try:
            cookie = SimpleCookie(handler.headers.get("Cookie", ""))
            token = cookie['power_demo_session'].value
            return hashlib.sha256(token.encode()).hexdigest()
        except (CookieError, KeyError):
            return ""

    def set_url(self, url):
        match = re.fullmatch(r"https://([a-z0-9-]+\.trycloudflare\.com)", url)
        if not match:
            raise ValueError("分享地址必须为 Cloudflare 临时 HTTPS 域名。")
        self.public_host = match[1]

    def check(self, handler, csrf_token):
        local = {f"127.0.0.1:{handler.server.server_port}", f"localhost:{handler.server.server_port}"}
        host = handler.headers.get("Host", "")
        if len(handler.headers.get_all("Host", [])) != 1 or host not in local | ({self.public_host} if self.public_host else set()):
            handler.send(403, {"error": "分享入口尚未就绪或地址无效。"})
            return False
        if host == self.public_host and handler.headers.get("X-Forwarded-Proto") != "https":
            handler.send(403, {"error": "请使用 HTTPS 分享地址。"})
            return False
        origin = handler.headers.get("Origin")
        expected_origin = ("http://" if host in local else "https://") + host
        if origin and origin != expected_origin:
            handler.send(403, {"error": "不允许跨站请求。"})
            return False
        path = urlsplit(handler.path).path
        if handler.command == "GET" and path in {"/login", "/login.css", "/login.js"}:
            filename, mime = {"/login": ("login.html", "text/html"), "/login.css": ("login.css", "text/css"), "/login.js": ("login.js", "text/javascript")}[path]
            handler.send(200, (Path(__file__).resolve().parent.parent / 'static' / filename).read_bytes(), mime + '; charset=utf-8')
            return False
        if handler.command == "POST" and path == "/api/auth/login":
            # An exact Origin and JSON request prevent cross-site login submissions.
            handler.close_connection = True
            if origin != expected_origin or handler.headers.get_content_type() != "application/json":
                handler.send(403, {"error": "登录请求无效，请刷新页面后重试。"})
                return False
            try:
                length = int(handler.headers.get("Content-Length", "0"))
                if not 0 < length <= 4096:
                    raise ValueError()
                body = json.loads(handler.rfile.read(length))
                if not isinstance(body, dict) or not isinstance(body.get('username'), str) or not isinstance(body.get('password'), str):
                    raise ValueError()
                credentials = (body['username'] + ':' + body['password']).encode()
            except (ValueError, TypeError, UnicodeError):
                handler.send(400, {"error": "请填写账号和访问密码。"})
                return False
            with self.lock:
                now = time.monotonic()
                self.failures = [t for t in self.failures if now - t < 60]
                if len(self.failures) >= 20:
                    handler.send(429, {"error": "尝试次数较多，请一分钟后再试。"}, headers={"Retry-After": "60"})
                    return False
                user = self.accounts.authenticate(body['username'], body['password']) if self.accounts else None
                if (self.accounts and user is None) or (not self.accounts and not secrets.compare_digest(credentials, self.expected)):
                    self.failures.append(now)
                    handler.send(401, {"error": "账号或密码不正确，请检查分享者提供的登录信息。"})
                    return False
                self.sessions = {k: t for k, t in self.sessions.items() if now < t['expires']}
                if len(self.sessions) >= 256:
                    handler.send(429, {"error": "当前登录人数较多，请稍后再试。"})
                    return False
                self.sessions.pop(self.session_key(handler), None)
                token = secrets.token_urlsafe(32)
                self.sessions[hashlib.sha256(token.encode()).hexdigest()] = {
                    'expires': now + 28800, 'user': user, 'csrf': secrets.token_urlsafe(32) if self.accounts else csrf_token}
            handler.send(200, {"ok": True}, headers={"Set-Cookie": self.cookie(token, host not in local)})
            return False
        key = self.session_key(handler)
        with self.lock:
            session = self.sessions.get(key)
            valid = session is not None and time.monotonic() < session['expires']
        if valid and self.accounts:
            user = self.accounts.get(session['user']['id'])
            valid = user is not None and user['active'] and user['version'] == session['user']['version']
            if valid:
                handler.user = user
                handler.session_csrf = session['csrf']
                if handler.headers.get('X-Workspace-User', user['id']) != user['id']:
                    handler.close_connection = True
                    handler.send(409, {'error':'当前浏览器已切换账号，请刷新页面。','code':'workspace_changed'})
                    return False
        if not valid:
            with self.lock:
                self.sessions.pop(key, None)
            handler.close_connection = True
            if handler.command == "GET" and not path.startswith('/api/'):
                handler.send(303, "", headers={"Location": "/login"})
            else:
                handler.send(401, {"error": "请先登录演示空间。", "code": "login_required"})
            return False
        if handler.command in {"POST", "DELETE"} and handler.headers.get("X-CSRF-Token") != session['csrf']:
            handler.send(403, {"error": "会话已失效，请刷新页面。"})
            return False
        if handler.command == 'POST' and path == '/api/auth/logout':
            with self.lock:
                self.sessions.pop(key, None)
            handler.close_connection = True
            handler.send(200, {"ok": True}, headers={"Set-Cookie": self.cookie('', host not in local, 0)})
            return False
        return True
