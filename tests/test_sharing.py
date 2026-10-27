import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.request
from unittest.mock import patch

from app import create_server
from power_agent.knowledge import KnowledgeBase
from power_agent.sharing import ShareAccess


class ShareAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.access = ShareAccess('test-password-32-characters-long!')
        with patch.dict('os.environ', {'LLM_API_URL': '', 'LLM_MODEL': ''}):
            self.server = create_server(0, Path(self.temp.name), knowledge_base=KnowledgeBase(Path(self.temp.name)/'demo.db'), access=self.access)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        code, headers, _ = self.call('/api/auth/login', auth=False, headers={'Origin':self.base},
            body={'username':'guest','password':'test-password-32-characters-long!'})
        self.assertEqual(code, 200)
        self.auth = headers['Set-Cookie'].split(';')[0]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def call(self, path='/api/status', *, auth=True, headers=None, body=None):
        fields = {'Content-Type':'application/json'}
        if auth:
            fields['Cookie'] = self.auth
        fields.update(headers or {})
        req = urllib.request.Request(self.base+path, headers=fields, data=json.dumps(body).encode() if body is not None else None)
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, response.headers, response.read()

    def test_no_anonymous_access_to_page_status_or_history(self):
        for path in ['/api/status', '/api/conversations', '/api/memories', '/api/documents']:
            code, headers, body = self.call(path, auth=False)
            self.assertEqual(code, 401)
            self.assertIsNone(headers['WWW-Authenticate'])
            self.assertNotIn('csrf_token', body.decode())
        self.assertEqual(self.call(auth=False, headers={'Cookie':'power_demo_session=forged'})[0], 401)
        for path in ['/', '/login', '/login.css', '/login.js']:
            code, _, body = self.call(path, auth=False)
            self.assertEqual(code, 200)
        self.assertIn('欢迎，开始探索。', self.call('/', auth=False)[2].decode())

    def test_login_cookie_logout_and_expiration(self):
        self.access.set_url('https://demo-test.trycloudflare.com')
        public = {'Host':'demo-test.trycloudflare.com', 'X-Forwarded-Proto':'https', 'Origin':'https://demo-test.trycloudflare.com'}
        code, headers, body = self.call('/api/auth/login', auth=False, headers=public,
            body={'username':'guest','password':'test-password-32-characters-long!'})
        self.assertEqual(code, 200)
        cookie = headers['Set-Cookie']
        for flag in ['HttpOnly','SameSite=Strict','Secure','Max-Age=28800']:
            self.assertIn(flag, cookie)
        self.assertNotIn('test-password', body.decode())
        csrf = json.loads(self.call()[2])['csrf_token']
        self.assertEqual(self.call('/api/auth/logout', body={})[0], 403)
        code, headers, _ = self.call('/api/auth/logout', body={}, headers={'Origin':self.base, 'X-CSRF-Token':csrf})
        self.assertEqual(code, 200)
        self.assertIn('Max-Age=0', headers['Set-Cookie'])
        self.assertEqual(self.call()[0], 401)
        self.auth = cookie.split(';')[0]
        self.assertEqual(self.call()[0], 200)
        with patch('power_agent.sharing.time.monotonic', return_value=10**15):
            self.assertEqual(self.call()[0], 401)

    def test_login_rejects_cross_origin_invalid_password_and_repeated_attempts(self):
        credentials = {'username':'guest','password':'wrong'}
        self.assertEqual(self.call('/api/auth/login', auth=False, body=credentials)[0], 403)
        self.assertEqual(self.call('/api/auth/login', auth=False, body=credentials, headers={'Origin':'https://evil.test'})[0], 403)
        self.assertEqual(self.call('/api/auth/login', auth=False, body=[], headers={'Origin':self.base})[0], 400)
        for _ in range(20):
            self.assertEqual(self.call('/api/auth/login', auth=False, body=credentials, headers={'Origin':self.base})[0], 401)
        self.assertEqual(self.call('/api/auth/login', auth=False, body=credentials, headers={'Origin':self.base})[0], 429)

    def test_public_host_https_and_csrf_are_required(self):
        public = {'Host':'demo-test.trycloudflare.com', 'X-Forwarded-Proto':'https'}
        self.assertEqual(self.call(headers=public)[0], 403)
        self.access.set_url('https://demo-test.trycloudflare.com')
        code, _, body = self.call(headers=public)
        self.assertEqual(code, 200)
        status = json.loads(body)
        self.assertTrue(status['shared_demo'])
        self.assertEqual(self.call(headers={'Host':public['Host']})[0], 403)
        self.assertEqual(self.call(headers={**public,'Origin':'https://attacker.test'})[0], 403)
        self.assertEqual(self.call('/api/conversations', headers=public, body={})[0], 403)
        valid = {**public, 'Origin':'https://demo-test.trycloudflare.com', 'X-CSRF-Token':status['csrf_token']}
        self.assertEqual(self.call('/api/conversations', headers=valid, body={})[0], 201)

    def test_cannot_configure_arbitrary_public_hosts(self):
        for url in ['http://example.com','https://demo.trycloudflare.com.attacker.test','https://demo.trycloudflare.com/path']:
            with self.assertRaises(ValueError):
                self.access.set_url(url)
