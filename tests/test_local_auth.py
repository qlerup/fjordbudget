import re
import tempfile
import unittest
from pathlib import Path
from app import create_app
from db import connect


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.config={'TESTING':True,'DATA_DIR':self.temp.name,'FJORDHUB_URL':'','FJORDHUB_API_KEY':'',
            'INSTALLATION_NAME':'Tommy Budget','TRUSTED_PROXY_IPS':'127.0.0.1,192.168.1.110'}
        self.app=create_app(self.config)
        self.client=self.app.test_client()
        self.password='My-private-budget-2026'

    def token(self,path='/setup',client=None,base_url=None):
        client=client or self.client
        reply=client.get(path,**({'base_url':base_url} if base_url else {}))
        return re.search(r'name="csrf" value="([^"]+)"',reply.text)[1]

    def setup(self,client=None,**changes):
        client=client or self.client
        data={'csrf':self.token(client=client),'username':'tommy','password':self.password,'password2':self.password,**changes}
        return client.post('/setup',data=data)

    def login(self,client=None,base_url=None,**changes):
        client=client or self.client
        return client.post('/login',data={'csrf':self.token('/login',client,base_url),
            'username':'tommy','password':self.password,**changes},**({'base_url':base_url} if base_url else {}))

    def test_auto_detection_and_first_setup_without_extra_token(self):
        self.assertEqual(self.app.config['AUTH_MODE'],'solo')
        self.assertEqual(self.client.get('/').location,'/setup')
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        page=self.client.get('/setup').text
        self.assertIn('Tommy Budget',page)
        self.assertIn('Første opsætning',page)
        self.assertNotIn('name="token"',page)
        self.assertEqual(self.setup().status_code,302)
        self.assertEqual(self.client.get('/setup').location,'/login')
        with connect(self.app.extensions['db_path']) as db:
            row=db.execute('SELECT * FROM local_owner').fetchone()
            self.assertNotEqual(row['password_hash'],self.password)
            self.assertEqual(row['username'],'tommy')
            self.assertEqual(db.execute("SELECT count(*) FROM accounts WHERE source='live'").fetchone()[0],0)
        self.assertTrue((Path(self.temp.name)/'local-install-initialized').exists())

    def test_setup_csrf_confirmation_and_second_browser_cannot_replace_owner(self):
        self.assertEqual(self.client.post('/setup',data={'username':'tommy','password':self.password}).status_code,403)
        self.assertEqual(self.setup(password2='Wrong').status_code,400)
        self.assertEqual(self.setup(password='short',password2='short').status_code,400)
        self.assertEqual(self.setup().status_code,302)
        other=self.app.test_client()
        self.assertEqual(other.post('/setup',data={'username':'intruder'}).location,'/login')
        self.assertEqual(self.login().status_code,302)
        self.assertEqual(self.client.get('/api/dashboard').status_code,200)
        self.assertEqual(other.get('/api/dashboard').status_code,401)

    def test_login_logout_persistence_expiry_and_deletion_do_not_reopen_setup(self):
        self.setup()
        self.assertEqual(self.login(password='wrong').status_code,401)
        self.assertEqual(self.login().status_code,302)
        self.assertIn('Log ud',self.client.get('/').text)
        self.assertTrue(self.client.get('/api/auth/access').json['authenticated'])
        csrf=self.client.get('/').text.split('name="csrf-token" content="')[1].split('"')[0]
        self.assertEqual(self.client.post('/logout').status_code,403)
        self.assertEqual(self.client.post('/logout',headers={'X-CSRF-Token':csrf}).status_code,200)
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        reopened=create_app(self.config).test_client()
        self.assertEqual(reopened.get('/').location,'/login')
        self.assertEqual(self.login().status_code,302)
        with self.client.session_transaction() as session:
            session['login_at']=0
        self.assertEqual(self.client.get('/api/dashboard').status_code,401)
        with connect(self.app.extensions['db_path']) as db:
            db.execute('DELETE FROM local_owner')
        self.assertEqual(self.client.get('/setup').status_code,503)

    def test_owner_login_accepts_new_domain_and_trusts_only_configured_proxy(self):
        self.setup()
        domain='https://tommy.example.test'
        self.assertEqual(self.login(base_url=domain).status_code,302)
        self.assertEqual(self.client.get('/api/config',base_url=domain).json['callback_url'],domain+'/bank/callback')
        with self.client.session_transaction() as session:
            session.clear()
        # HTTPS offload is accepted only from the configured proxy address.
        result=self.client.get('/login',headers={'Host':'tommy.example.test','X-Forwarded-Proto':'https'},
            environ_overrides={'REMOTE_ADDR':'192.168.1.110'})
        self.assertIn('Secure',result.headers.get('Set-Cookie',''))
        other=self.app.test_client()
        result=other.get('/login',headers={'X-Forwarded-Proto':'https'},environ_overrides={'REMOTE_ADDR':'192.168.1.50'})
        self.assertNotIn('Secure;',result.headers.get('Set-Cookie',''))

    def test_auto_managed_configuration_skips_local_setup_and_partial_config_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            managed=create_app({'TESTING':True,'DATA_DIR':directory,'FJORDHUB_URL':'http://hub.test','FJORDHUB_API_KEY':'test-key'})
            self.assertEqual(managed.config['AUTH_MODE'],'fjordhub')
            self.assertNotIn('setup',managed.view_functions)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                create_app({'TESTING':True,'DATA_DIR':directory,'FJORDHUB_URL':'http://hub.test','FJORDHUB_API_KEY':''})

    def test_login_rate_limit_is_persistent(self):
        self.setup()
        for _ in range(10):
            self.assertEqual(self.login(password='wrong').status_code,401)
        self.assertEqual(self.login().status_code,429)
        reopened=create_app(self.config).test_client()
        self.assertEqual(self.login(reopened).status_code,429)
