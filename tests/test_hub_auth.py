import re
import tempfile
import unittest
from unittest.mock import patch

from app import create_app


class HubAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.app=create_app({'TESTING':True,'DATA_DIR':self.temp.name,'AUTH_MODE':'fjordhub',
            'FJORDHUB_URL':'http://hub.test','FJORDHUB_API_KEY':'test-key'})
        self.client=self.app.test_client()
        self.users=[{'id':7,'username':'owner','role':'admin'}]
        self.external='https://budget.example.org'
        self.mock=patch('hub_auth.requests.request',side_effect=self.hub)
        self.mock.start();self.addCleanup(self.mock.stop)

    def hub(self, method, url, **kwargs):
        from unittest.mock import Mock
        if url.endswith('/config'): data={'ok':True,'external_url':self.external}
        elif url.endswith('/users'): data={'ok':True,'items':self.users}
        elif url.endswith('/sso-verify'): data={'ok':True,**self.users[0]} if kwargs['params']['token']=='valid' else {'ok':False}
        else: data={'ok':True,'user':self.users[0]} if kwargs['json'].get('password')=='correct' else {'ok':False}
        return Mock(status_code=200,ok=True,json=lambda:data)

    def login(self):
        return self.client.get('/hub-login?token=valid')

    def test_public_policy_and_private_api(self):
        for path in ('/privacy','/terms','/api/health','/login'):
            self.assertEqual(self.client.get(path).status_code,200)
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        self.assertEqual(self.client.get('/api/config').status_code,401)
        self.assertEqual(self.client.post('/api/bank/credentials',json={}).status_code,401)
        self.assertEqual(self.client.get('/').location,'/login')

    def test_sso_owner_revocation_and_second_owner_denied(self):
        self.assertEqual(self.login().location,'/')
        self.assertEqual(self.client.get('/api/dashboard').status_code,200)
        self.users=[{'id':8,'username':'other','role':'admin'}]
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        self.assertEqual(self.login().location,'/login')
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)

    def test_first_user_must_be_admin_and_bad_sso_denied(self):
        self.assertEqual(self.client.get('/hub-login?token=wrong').location,'/login')
        self.users[0]['role']='user'
        self.assertEqual(self.login().location,'/login')

    def test_password_login_csrf_logout_and_throttle(self):
        self.client.get('/login')
        with self.client.session_transaction() as s: token=s['csrf']
        self.assertEqual(self.client.post('/login',data={'username':'owner','password':'correct'}).status_code,403)
        self.assertEqual(self.client.post('/login',data={'username':'owner','password':'correct','csrf':token}).location,'/')
        with self.client.session_transaction() as s: token=s['csrf']
        self.assertEqual(self.client.post('/logout',headers={'X-CSRF-Token':token}).status_code,200)
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        self.client.get('/login')
        with self.client.session_transaction() as s: token=s['csrf']
        for _ in range(5):
            response=self.client.post('/login',data={'username':'owner','password':'wrong','csrf':token})
            self.assertEqual(response.status_code,200)
        response=self.client.post('/login',data={'username':'owner','password':'correct','csrf':token})
        self.assertEqual(response.status_code,429)

    def test_public_domain_cookie_and_callback_follow_hub(self):
        response=self.client.get('/hub-login?token=valid',base_url=self.external)
        self.assertIn('Secure',response.headers['Set-Cookie'])
        config=self.client.get('/api/config',base_url=self.external).json
        self.assertEqual(config['callback_url'],self.external+'/bank/callback')
        self.assertEqual(self.client.get('/',base_url='https://evil.example').status_code,400)
        with patch('hub_auth.requests.request',side_effect=__import__('requests').ConnectionError()):
            self.assertEqual(self.client.get('/api/dashboard',base_url=self.external).status_code,503)
