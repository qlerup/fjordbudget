import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from app import create_app


class CredentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.pem = cls.key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                        serialization.NoEncryption()).decode()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'TESTING': True, 'DATA_DIR': self.temp.name}
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', self.client.get('/').text)[1]
        self.headers = {'X-CSRF-Token': csrf}
        self.data = {'app_id': 'b19f7346-09da-4a3d-89a3-91f666dd99bf', 'private_key': self.pem}

    def test_encrypted_persistence_and_immediate_signed_requests(self):
        response = self.client.post('/api/bank/credentials', json=self.data, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('PRIVATE KEY', response.text)
        stored = (Path(self.temp.name)/'bank-credentials.enc').read_bytes()
        self.assertNotIn(self.pem.encode(), stored)
        self.assertNotIn(self.data['app_id'].encode(), stored)
        for app in (self.app, create_app(self.config)):
            provider = app.extensions['bank_provider']
            self.assertTrue(provider.configured)
            with patch('banking.requests.request') as request:
                request.return_value.status_code = 200
                request.return_value.ok = True
                request.return_value.json.return_value = {'aspsps': []}
                provider.banks()
                token = request.call_args.kwargs['headers']['Authorization'].removeprefix('Bearer ')
                self.assertEqual(jwt.get_unverified_header(token)['kid'], self.data['app_id'])
                jwt.decode(token, self.key.public_key(), algorithms=['RS256'], audience='api.enablebanking.com')
            self.assertNotIn('PRIVATE KEY', app.test_client().get('/api/config').text)

    def test_invalid_input_and_csrf_cannot_overwrite_saved_key(self):
        self.client.post('/api/bank/credentials', json=self.data, headers=self.headers)
        path = Path(self.temp.name)/'bank-credentials.enc'
        before = path.read_bytes()
        for data in ([], {}, {**self.data, 'app_id': 'bad'}, {**self.data, 'private_key': 'not a key'}):
            self.assertEqual(self.client.post('/api/bank/credentials', json=data, headers=self.headers).status_code, 400)
            self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.client.post('/api/bank/credentials', json=self.data).status_code, 403)
        self.assertEqual(self.client.post('/api/bank/credentials', json=self.data,
            headers={**self.headers, 'Origin': 'https://example.org'}).status_code, 403)
        self.assertEqual(path.read_bytes(), before)

    def test_write_failure_preserves_previous_credentials(self):
        self.client.post('/api/bank/credentials', json=self.data, headers=self.headers)
        path = Path(self.temp.name)/'bank-credentials.enc'
        before = path.read_bytes()
        with patch('app.os.replace', side_effect=OSError('disk error')):
            response = self.client.post('/api/bank/credentials', json=self.data, headers=self.headers)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(Path(self.temp.name).glob('.bank-credentials-*')), [])
