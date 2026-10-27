"""Durable single-user conversation and cross-session memory store.

Uses the knowledge store's PostgreSQL schema (SQLite only in compatibility mode).
Only completed turns are committed; request IDs make a retry idempotent.
"""
import json
import re
import threading
import uuid
from datetime import datetime, timezone


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("会话或消息 ID 无效。")
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


class ConversationStore:
    def __init__(self, kb):
        self.kb = kb
        self.lock = threading.RLock()
        self.postgres = hasattr(kb, "embeddings")
        with kb.connect() as db:
            self.execute(db, """CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            self.execute(db, """CREATE TABLE IF NOT EXISTS conversation_turns (
                request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
                ordinal INTEGER NOT NULL, user_text TEXT NOT NULL, images TEXT NOT NULL,
                response TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(session_id, ordinal))""")
            self.execute(db, "CREATE INDEX IF NOT EXISTS conversation_turns_session ON conversation_turns(session_id, ordinal)")
            self.execute(db, """CREATE TABLE IF NOT EXISTS user_memories (
                id TEXT PRIMARY KEY, content TEXT NOT NULL, source_session TEXT NOT NULL,
                updated_at TEXT NOT NULL)""")

    def execute(self, db, statement, parameters=()):
        return db.execute(statement.replace("?", "%s") if self.postgres else statement, parameters)

    def create(self):
        with self.lock, self.kb.connect() as db:
            if self.execute(db, "SELECT COUNT(*) AS n FROM conversations").fetchone()["n"] >= 100:
                raise ValueError("最多保存 100 个会话，请先删除不需要的会话。")
            sid, timestamp = uuid.uuid4().hex, now()
            self.execute(db, "INSERT INTO conversations VALUES (?,?,'',?,?)", (sid, "新对话", timestamp, timestamp))
        return {"id": sid, "title": "新对话"}

    def list_sessions(self):
        with self.kb.connect() as db:
            return [dict(r) for r in self.execute(db, "SELECT id,title,created_at,updated_at FROM conversations ORDER BY updated_at DESC,id").fetchall()]

    def session(self, sid, before=None):
        identifier(sid)
        with self.kb.connect() as db:
            session = self.execute(db, "SELECT * FROM conversations WHERE id=?", (sid,)).fetchone()
            if not session:
                raise KeyError("会话不存在，请新建对话。")
            rows = self.execute(db, "SELECT * FROM conversation_turns WHERE session_id=? AND ordinal<? ORDER BY ordinal DESC LIMIT 10",
                                (sid, int(before) if before is not None else 1000000)).fetchall()
            turns = [dict(r) for r in reversed(rows)]
            for turn in turns:
                turn["images"] = json.loads(turn["images"])
                turn["response"] = json.loads(turn["response"])
            return dict(session, turns=turns, has_older=bool(turns and turns[0]["ordinal"] > 1))

    def cached(self, sid, request_id, message, images):
        with self.kb.connect() as db:
            row = self.execute(db, "SELECT * FROM conversation_turns WHERE request_id=?", (request_id,)).fetchone()
        if row:
            if row["session_id"] != sid or row["user_text"] != message or json.loads(row["images"]) != images:
                raise ValueError("消息 ID 已被其他请求使用，请新建一条消息。")
            return json.loads(row["response"])

    def user_turn(self, sid, ordinal=1):
        """Read an exact user message, independently of the recent-context window."""
        identifier(sid)
        with self.kb.connect() as db:
            if ordinal == -1:
                row = self.execute(db, "SELECT ordinal,user_text,images <> '[]' AS has_images FROM conversation_turns WHERE session_id=? ORDER BY ordinal DESC LIMIT 1", (sid,)).fetchone()
            else:
                row = self.execute(db, "SELECT ordinal,user_text,images <> '[]' AS has_images FROM conversation_turns WHERE session_id=? AND ordinal=?", (sid, ordinal)).fetchone()
            return dict(row) if row else None

    def memories(self):
        with self.kb.connect() as db:
            return [dict(r) for r in self.execute(db, "SELECT * FROM user_memories ORDER BY updated_at DESC,id").fetchall()]

    def save_memory(self, memory_id, content):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 300:
            raise ValueError("记忆内容应为 1～300 字。")
        if memory_id:
            identifier(memory_id)
        with self.lock, self.kb.connect() as db:
            if memory_id:
                if not self.execute(db, "UPDATE user_memories SET content=?,updated_at=? WHERE id=?", (content.strip(), now(), memory_id)).rowcount:
                    raise KeyError("记忆不存在。")
            else:
                if self.execute(db, "SELECT count(*) AS n FROM user_memories").fetchone()["n"] >= 50:
                    raise ValueError("最多保存 50 条长期记忆，请编辑或删除已有记忆。")
                memory_id = uuid.uuid4().hex
                self.execute(db, "INSERT INTO user_memories VALUES (?,?,?,?)", (memory_id, content.strip(), "manual", now()))
        return {"id": memory_id}

    def delete_memory(self, memory_id=None):
        with self.lock, self.kb.connect() as db:
            if memory_id:
                identifier(memory_id)
                self.execute(db, "DELETE FROM user_memories WHERE id=?", (memory_id,))
            else:
                self.execute(db, "DELETE FROM user_memories")

    def delete_session(self, sid):
        identifier(sid)
        with self.lock, self.kb.connect() as db:
            self.execute(db, "DELETE FROM conversation_turns WHERE session_id=?", (sid,))
            if not self.execute(db, "DELETE FROM conversations WHERE id=?", (sid,)).rowcount:
                raise KeyError("会话不存在。")
            self.execute(db, "DELETE FROM user_memories WHERE source_session=?", (sid,))

    def commit(self, sid, request_id, message, images, result, summary, memories):
        with self.lock, self.kb.connect() as db:
            session = self.execute(db, "SELECT id FROM conversations WHERE id=?", (sid,)).fetchone()
            if not session:
                raise KeyError("会话已删除，消息未保存。")
            count = self.execute(db, "SELECT count(*) AS n,coalesce(sum(length(images)),0) AS image_bytes FROM conversation_turns WHERE session_id=?", (sid,)).fetchone()
            encoded_images = json.dumps(images)
            if count["n"] >= 500 or count["image_bytes"] + len(encoded_images) > 100_000_000:
                raise ValueError("此会话已达到 500 轮或 100 MB 图片限制，请新建对话。")
            timestamp = now()
            saved = []
            for item in memories:
                # Exact user quotes only; the planner cannot manufacture memory text.
                text = item.get("content", "")
                if not isinstance(text, str) or not 2 <= len(text) <= 300 or text not in message:
                    continue
                if re.search(r"sk-[A-Za-z0-9]|密码|密钥|身份证|银行卡|password|api.?key", text, re.I):
                    continue
                if self.execute(db, "SELECT 1 FROM user_memories WHERE content=?", (text,)).fetchone():
                    continue
                if self.execute(db, "SELECT count(*) AS n FROM user_memories").fetchone()["n"] >= 50:
                    break
                mid = uuid.uuid4().hex
                self.execute(db, "INSERT INTO user_memories VALUES (?,?,?,?)", (mid, text, sid, timestamp))
                saved.append({"id": mid, "content": text})
            result["saved_memories"] = saved
            self.execute(db, "INSERT INTO conversation_turns VALUES (?,?,?,?,?,?,?)",
                         (request_id, sid, count["n"] + 1, message, encoded_images, json.dumps(result, ensure_ascii=False), timestamp))
            self.execute(db, "UPDATE conversations SET title=?,summary=?,updated_at=? WHERE id=?",
                         ((message or "图片对话")[:40] if count["n"] == 0 else self.execute(db, "SELECT title FROM conversations WHERE id=?", (sid,)).fetchone()["title"],
                          summary[:2000], timestamp, sid))
        return result
