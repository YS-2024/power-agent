"""Real PostgreSQL + HTTP isolation tests; random schemas, no paid API calls."""
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch, PropertyMock

import httpx
import psycopg
from psycopg import sql

from app import create_server
from power_agent.accounts import AccountStore
from power_agent.agent import PowerAgent
from power_agent.conversations import ConversationStore
from power_agent.postgres import PostgresKnowledgeBase
from power_agent.sharing import ShareAccess
from test_postgres import FakeEmbeddings


@unittest.skipUnless(os.getenv('POSTGRES_TEST_URL'), 'Requires isolated PostgreSQL test database')
class MultiUserTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.dsn = os.environ['POSTGRES_TEST_URL']
        self.schema = 'test_multi_' + uuid.uuid4().hex
        self.addCleanup(self.clean_database)
        self.kb = PostgresKnowledgeBase(self.dsn, FakeEmbeddings(), schema=self.schema)
        self.kb.mark_initialized()
        self.public_doc = self.kb.add('public.md', '# 公共电力\n授权共享的电力知识。')['id']
        legacy = ConversationStore(self.kb)
        legacy.create()
        legacy.save_memory(None, '旧共享空间的记忆不能继承给新账号')
        self.accounts = AccountStore(self.kb)
        self.accounts.create('admin', 'Admin-password-123', 'admin')
        self.accounts.create('alice', 'Alice-password-123')
        self.accounts.create('bob', 'Bob-password-123')
        with patch.dict(os.environ, {'LLM_API_URL':'','LLM_MODEL':''}):
            self.server = create_server(0, Path(self.folder.name), knowledge_base=self.kb,
                                        access=ShareAccess(accounts=self.accounts))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.admin = self.login('admin','Admin-password-123')
        self.alice = self.login('alice','Alice-password-123')
        self.bob = self.login('bob','Bob-password-123')

    def close_server(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def clean_database(self):
        with psycopg.connect(self.dsn) as db:
            if hasattr(self, 'accounts'):
                for row in self.accounts.list():
                    uid = row['id']
                    assert len(uid) == 32 and all(c in '0123456789abcdef' for c in uid)
                    db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier('account_' + uid)))
            assert self.schema.startswith('test_multi_') and len(self.schema) == 43
            db.execute(sql.SQL('DROP SCHEMA IF EXISTS {} CASCADE').format(sql.Identifier(self.schema)))

    def login(self, username, password):
        client = httpx.Client(base_url=self.base, timeout=15, trust_env=False)
        self.addCleanup(client.close)
        response = client.post('/api/auth/login', json={'username':username,'password':password}, headers={'Origin':self.base})
        self.assertEqual(response.status_code,200,response.text)
        status = client.get('/api/status').json()
        return {'client':client,'csrf':status['csrf_token'],'id':status['user']['id']}

    def call(self, who, path, body=None, method=None, headers=None):
        return who['client'].request(method or ('POST' if body is not None else 'GET'), path, json=body,
            headers={'Origin':self.base,'X-CSRF-Token':who['csrf'],'X-Workspace-User':who['id'],**(headers or {})})

    def test_private_chats_memories_sources_reports_and_ids_are_isolated(self):
        for who in [self.admin,self.alice,self.bob]:
            self.assertEqual(self.call(who,'/api/conversations').json(),[])
            self.assertEqual(self.call(who,'/api/memories').json(),[])
        sid = self.call(self.alice,'/api/conversations',{}).json()['id']
        mid = self.call(self.alice,'/api/memories',{'content':'我是 Alice，只能本人看到'}).json()['id']
        own_doc = self.call(self.alice,'/api/documents',{'filename':'alice.md','content':'# Alice 电力私密资料\nALICE-SECRET-773'}).json()['id']
        self.assertEqual(self.call(self.alice,'/api/assistant',{'session_id':sid,'request_id':uuid.uuid4().hex,'message':'我第一个问题是什么'}) .status_code,200)
        for who in [self.bob,self.admin]:
            self.assertEqual(self.call(who,'/api/conversations/'+sid).status_code,404)
            self.assertEqual(self.call(who,'/api/conversations/'+sid,method='DELETE').status_code,404)
            self.assertEqual(self.call(who,'/api/assistant/cancel',{'session_id':sid,'request_id':uuid.uuid4().hex}).status_code,404)
            self.assertEqual(self.call(who,'/api/memories',{'id':mid,'content':'越权修改'}).status_code,404)
            self.assertEqual(self.call(who,'/api/documents/'+own_doc).status_code,404)
            self.assertEqual(self.call(who,'/api/documents/'+own_doc,method='DELETE').status_code,404)
            sources = self.call(who,'/api/search',{'question':'Alice 电力私密资料'}).json()['sources']
            self.assertNotIn(own_doc,[s['document_id'] for s in sources])
            self.assertNotIn('ALICE-SECRET-773',json.dumps(sources))
        self.call(self.bob,'/api/memories',method='DELETE')
        self.assertEqual(len(self.call(self.alice,'/api/memories').json()),1)
        report = self.call(self.alice,'/api/report',{'question':'Alice 电力私密资料'}).json()
        self.assertEqual(self.call(self.alice,report['report_url']).status_code,200)
        self.assertEqual(self.call(self.bob,report['report_url']).status_code,404)

    def test_public_permissions_admin_management_and_spoofed_owner(self):
        self.assertEqual(self.call(self.bob,'/api/users').status_code,403)
        self.assertEqual(self.call(self.bob,'/api/users',{'username':'hacker','password':'hacker-password','role':'admin'}).status_code,403)
        self.assertEqual(self.call(self.bob,'/api/users/'+self.admin['id'],{'password':'hacker-password'}).status_code,403)
        with patch.object(self.kb.embeddings,'embed_documents') as embed:
            self.assertEqual(self.call(self.bob,'/api/documents',{'scope':'public','filename':'bad.md','content':'越权上传'}).status_code,403)
        embed.assert_not_called()
        self.assertEqual(self.call(self.bob,'/api/documents/'+self.public_doc,method='DELETE').status_code,403)
        private_doc = self.call(self.bob,'/api/documents',{'filename':'own.md','content':'# Bob 电力资料','user_id':self.alice['id']}).json()['id']
        self.assertEqual(self.call(self.alice,'/api/documents/'+private_doc).status_code,404)
        self.assertEqual(self.call(self.admin,'/api/users/'+self.bob['id'],{'can_read_public':False}).status_code,200)
        self.assertEqual(self.call(self.bob,'/api/status').status_code,401)
        bob = self.login('bob','Bob-password-123')
        self.assertEqual(self.call(bob,'/api/documents/'+self.public_doc).status_code,404)
        self.assertEqual([d['id'] for d in self.call(bob,'/api/documents').json()],[private_doc])
        self.assertNotIn(self.public_doc,[s['document_id'] for s in self.call(bob,'/api/search',{'question':'电力'}).json()['sources']])
        added = self.call(self.admin,'/api/documents',{'scope':'public','filename':'more.md','content':'# 新公共电力资料'}).json()['id']
        self.assertEqual(self.call(self.alice,'/api/documents/'+added).status_code,200)
        self.assertEqual(self.call(self.admin,'/api/documents/'+added,method='DELETE').status_code,200)
        created = self.call(self.admin,'/api/users',{'username':'charlie','password':'Charlie-password-123'}).json()
        self.assertEqual(created['role'],'member')
        self.assertNotIn('password_hash',json.dumps(self.call(self.admin,'/api/users').json()))

    def test_password_changes_reset_disable_and_persistence(self):
        self.assertEqual(self.call(self.alice,'/api/auth/password',{'current_password':'wrong','new_password':'Alice-new-password-123'}).status_code,400)
        self.assertEqual(self.call(self.alice,'/api/auth/password',{'current_password':'Alice-password-123','new_password':'Alice-new-password-123'}).status_code,200)
        self.assertEqual(self.call(self.alice,'/api/status').status_code,401)
        reopened = AccountStore(self.kb)
        self.assertIsNone(reopened.authenticate('alice','Alice-password-123'))
        self.assertEqual(reopened.authenticate('alice','Alice-new-password-123')['id'],self.alice['id'])
        alice = self.login('alice','Alice-new-password-123')
        self.assertEqual(self.call(self.admin,'/api/users/'+alice['id'],{'password':'Alice-reset-password-123'}).status_code,200)
        self.assertEqual(self.call(alice,'/api/status').status_code,401)
        self.assertEqual(self.call(self.admin,'/api/users/'+alice['id'],{'active':False}).status_code,200)
        self.assertIsNone(reopened.authenticate('alice','Alice-reset-password-123'))
        self.assertEqual(self.call(self.admin,'/api/users/'+alice['id'],{'active':True}).status_code,200)
        self.login('alice','Alice-reset-password-123')
        self.assertEqual(self.call(self.admin,'/api/users/'+self.admin['id'],{'active':False}).status_code,400)
        with self.kb.connect() as db:
            hashes = [r['password_hash'] for r in db.execute('SELECT password_hash FROM accounts').fetchall()]
        self.assertTrue(all(h.startswith('pbkdf2_sha256$600000$') for h in hashes))
        self.assertNotIn('Alice-reset-password-123',' '.join(hashes))

    def test_sessions_csrf_and_same_browser_account_switch(self):
        self.assertNotEqual(self.alice['csrf'],self.bob['csrf'])
        self.assertEqual(self.call(self.bob,'/api/conversations',{},headers={'X-CSRF-Token':self.alice['csrf']}).status_code,403)
        self.assertEqual(self.call(self.bob,'/api/status',headers={'X-Workspace-User':self.alice['id']}).status_code,409)
        self.assertEqual(self.call(self.bob,'/api/conversations').json(),[])
        self.assertIsNone(self.accounts.authenticate('guest','old-shared-password'))

    def test_concurrent_users_have_separate_generation_locks_and_memory_context(self):
        self.call(self.alice,'/api/memories',{'content':'ALICE_ONLY_PREF'})
        self.call(self.bob,'/api/memories',{'content':'BOB_ONLY_PREF'})
        sid_a=self.call(self.alice,'/api/conversations',{}).json()['id']
        sid_b=self.call(self.bob,'/api/conversations',{}).json()['id']
        barrier=threading.Barrier(2)
        prompts=[]
        def model(messages):
            prompts.append(json.dumps(messages,ensure_ascii=False))
            barrier.wait(timeout=5)
            return '我是电知。'
        with patch.object(PowerAgent,'model_enabled',new_callable=PropertyMock,return_value=True), patch.object(PowerAgent,'_complete',side_effect=model), ThreadPoolExecutor() as pool:
            jobs=[pool.submit(self.call,who,'/api/assistant',{'session_id':sid,'request_id':uuid.uuid4().hex,'message':'你是谁'}) for who,sid in [(self.alice,sid_a),(self.bob,sid_b)]]
            self.assertEqual([j.result(timeout=10).status_code for j in jobs],[200,200])
        self.assertEqual(len(prompts),2)
        self.assertTrue(all(('ALICE_ONLY_PREF' in p) != ('BOB_ONLY_PREF' in p) for p in prompts))
