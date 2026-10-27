"""Persistent accounts and per-account workspaces for the external service."""
import hashlib
import re
import secrets
import threading
import uuid

from .agent import PowerAgent
from .assistant import ConversationalAssistant
from .postgres import PostgresKnowledgeBase, SearchResults


def password_hash(password):
    if not isinstance(password, str) or not 12 <= len(password) <= 128:
        raise ValueError('密码须为 12～128 个字符。')
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), 600000).hex()
    return f'pbkdf2_sha256$600000${salt}${digest}'


def password_matches(password, encoded):
    if not isinstance(password, str) or len(password) > 128:
        return False
    algorithm, rounds, salt, expected = encoded.split('$')
    if algorithm != 'pbkdf2_sha256':
        return False
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds)).hex()
    return secrets.compare_digest(actual, expected)


class AccountStore:
    def __init__(self, kb):
        self.kb = kb
        self.dummy_hash = password_hash(secrets.token_urlsafe(24))
        with kb.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS accounts (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin','member')), active BOOLEAN NOT NULL DEFAULT TRUE,
                can_read_public BOOLEAN NOT NULL DEFAULT TRUE, version INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now())''')

    @staticmethod
    def public(row):
        return {k: row[k] for k in ('id','username','role','active','can_read_public','version')}

    def list(self):
        with self.kb.connect() as db:
            return [self.public(row) for row in db.execute('SELECT * FROM accounts ORDER BY created_at,id').fetchall()]

    def get(self, uid):
        with self.kb.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=%s', (uid,)).fetchone()
        return self.public(row) if row else None

    def authenticate(self, username, password):
        name = username.strip().lower() if isinstance(username, str) else ''
        with self.kb.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE username=%s', (name,)).fetchone()
        valid = password_matches(password, row['password_hash'] if row else self.dummy_hash)
        return self.public(row) if valid and row and row['active'] else None

    def create(self, username, password, role='member', can_read_public=True):
        if not isinstance(username, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_.-]{2,31}', username):
            raise ValueError('账号须为 3～32 位英文字母、数字、点、下划线或短横线，并以字母开头。')
        if role not in {'admin','member'} or not isinstance(can_read_public, bool):
            raise ValueError('账号角色或权限无效。')
        name, uid, encoded = username.lower(), uuid.uuid4().hex, password_hash(password)
        with self.kb.connect() as db:
            db.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (self.kb.schema + ':accounts',))
            if db.execute('SELECT 1 FROM accounts WHERE username=%s', (name,)).fetchone():
                raise ValueError('该账号已存在。')
            if db.execute('SELECT count(*) AS n FROM accounts').fetchone()['n'] >= 100:
                raise ValueError('最多支持 100 个账号。')
            db.execute('INSERT INTO accounts(id,username,password_hash,role,can_read_public) VALUES (%s,%s,%s,%s,%s)',
                       (uid, name, encoded, role, True if role == 'admin' else can_read_public))
        return self.get(uid)

    def update(self, actor, uid, *, active=None, can_read_public=None, password=None):
        if actor['role'] != 'admin':
            raise PermissionError('仅管理员可以管理账号。')
        if active is not None and not isinstance(active, bool) or can_read_public is not None and not isinstance(can_read_public, bool):
            raise ValueError('权限值须为布尔值。')
        if uid == actor['id'] and active is False:
            raise ValueError('不能禁用当前管理员账号。')
        encoded = password_hash(password) if password is not None else None
        with self.kb.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=%s FOR UPDATE', (uid,)).fetchone()
            if not row:
                raise KeyError('账号不存在。')
            if row['role'] == 'admin' and can_read_public is False:
                raise ValueError('管理员须保留公共资料管理权限。')
            db.execute('''UPDATE accounts SET active=%s,can_read_public=%s,password_hash=%s,version=version+1 WHERE id=%s''',
                       (row['active'] if active is None else active, row['can_read_public'] if can_read_public is None else can_read_public,
                        encoded or row['password_hash'], uid))
        return self.get(uid)

    def change_password(self, user, current, new):
        encoded = password_hash(new)
        with self.kb.connect() as db:
            row = db.execute('SELECT * FROM accounts WHERE id=%s FOR UPDATE', (user['id'],)).fetchone()
            if not row or not row['active'] or not password_matches(current, row['password_hash']):
                raise ValueError('当前密码不正确。')
            db.execute('UPDATE accounts SET password_hash=%s,version=version+1 WHERE id=%s', (encoded, user['id']))


class UserKnowledge:
    """Only the owner's schema and permitted public library can enter retrieval."""
    backend = 'PostgreSQL + pgvector'

    def __init__(self, private, public, user):
        self.private, self.public, self.user = private, public, dict(user)
        self.embeddings = private.embeddings
        self.connect = private.connect

    def libraries(self):
        return [(self.private, 'private')] + ([(self.public, 'public')] if self.user['can_read_public'] else [])

    def describe(self, doc, scope):
        return dict(doc, scope=scope, can_delete=scope == 'private' or self.user['role'] == 'admin')

    @property
    def chunk_count(self):
        return sum(kb.chunk_count for kb, _ in self.libraries())

    def list_documents(self):
        return [self.describe(d, scope) for kb, scope in self.libraries() for d in kb.list_documents()]

    def get(self, doc_id):
        for kb, scope in self.libraries():
            try:
                return self.describe(kb.get(doc_id), scope)
            except KeyError:
                pass
        raise KeyError('资料不存在。')

    def for_import(self, scope):
        if scope == 'private':
            return self.private
        if scope == 'public' and self.user['role'] == 'admin':
            return self.public
        raise PermissionError('你没有该资料库的写入权限。')

    def delete(self, doc_id):
        doc = self.get(doc_id)
        if not doc['can_delete']:
            raise PermissionError('公共资料仅管理员可以删除。')
        self.for_import(doc['scope']).delete(doc_id)

    def search(self, query, limit=5):
        rows, notices = [], []
        for kb, scope in self.libraries():
            result = kb.search(query, limit=limit)
            rows.extend(dict(row, scope=scope) for row in result)
            if result.notice and result.notice not in notices:
                notices.append(result.notice)
        rows.sort(key=lambda r: (-r['score'], r['chunk_id']))
        return SearchResults([dict(row, citation=f'S{i}') for i, row in enumerate(rows[:limit],1)],
                             notice=' '.join(notices), mode='lexical' if notices else 'hybrid')


class WorkspaceManager:
    def __init__(self, public, root):
        self.public, self.root = public, root
        self.workspaces = {}
        self.user_locks = {}
        self.lock = threading.Lock()

    def get(self, user):
        key = (user['id'], user['role'], user['can_read_public'])
        with self.lock:
            if key not in self.workspaces:
                if not re.fullmatch(r'[a-f0-9]{32}', user['id']):
                    raise ValueError('账号 ID 无效。')
                private = PostgresKnowledgeBase(self.public.dsn, self.public.embeddings,
                    schema='account_' + user['id'], min_similarity=self.public.min_similarity)
                private.mark_initialized()
                kb = UserKnowledge(private, self.public, user)
                agent = PowerAgent(kb, self.root / 'users' / user['id'] / 'reports')
                assistant = ConversationalAssistant(agent)
                assistant.store.lock = self.user_locks.setdefault(user['id'], threading.RLock())
                self.workspaces[key] = (kb, agent, assistant)
            return self.workspaces[key]
