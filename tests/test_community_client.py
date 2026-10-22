"""Client contract checks independent of the privately operated server."""
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from app import create_app
from community_crypto import public_key
from community_protocol import CONSENT_VERSION
from db import connect


class CommunityClientTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = create_app({'TESTING': True, 'AUTH_MODE': 'local', 'DATA_DIR': directory.name,
            'COMMUNITY_SERVER_URL': 'https://community.example.test',
            'COMMUNITY_SERVER_PUBLIC_KEY': public_key(X25519PrivateKey.generate())})
        self.path = self.app.extensions['db_path']
        self.sync = self.app.extensions['community_sync']
        self.calls = []
        self.canonical = None
        with connect(self.path) as db:
            db.execute("INSERT INTO accounts(id,source,name,bank,last4,currency) VALUES ('real','live','Private','Test','1234','DKK')")

        def transport(method, endpoint, token, payload=None):
            self.calls.append((method, endpoint, payload))
            if endpoint == '/v1/contributions':
                return {'items': [{'bank_text': item['bank_text'], 'status': 'pending'} for item in payload['items']]}
            if endpoint.startswith('/v1/rules'):
                return {'items': [], 'cursor': 0}
            if endpoint == '/v1/resolutions':
                return {'items': [{**item, 'status': 'merged' if self.canonical else 'pending',
                    'canonical_merchant': self.canonical} for item in payload['items']]}
            return {'ok': True}
        self.sync._call = transport

    def seed(self, text='POS EXAMPLE SHOP', merchant='Local shop', manual=True, remember=True, category='Fritid'):
        with connect(self.path) as db:
            row = db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,description,
                amount,currency,category,merchant,merchant_manual) VALUES ('real',?,'2026-10-01',?,-12345,'DKK',?,?,?)''',
                (str(uuid.uuid4()), text, category, merchant, int(manual)))
            if remember:
                db.execute("INSERT OR REPLACE INTO merchant_rules VALUES ('live',?,?)", (text.casefold(), merchant))
            return row.lastrowid

    def test_sharing_defaults_off_and_filters_unconfirmed_or_private_texts(self):
        self.seed()
        self.seed('POS INFERRED SHOP', manual=False)
        self.seed('POS FORGOTTEN SHOP', remember=False)
        self.seed('MobilePay Private Person')
        self.seed('POS 1234567890 Private')
        self.seed('POS TRANSFER SHOP', category='Overførsler')
        self.assertFalse(self.sync.status()['enabled'])
        self.assertEqual(self.sync.status()['eligible'], 1)
        self.assertTrue(self.sync.sync_once())
        self.assertEqual(self.calls, [])
        self.sync.configure(True, CONSENT_VERSION)
        self.assertTrue(self.sync.sync_once())
        items = next(payload['items'] for _, endpoint, payload in self.calls if endpoint == '/v1/contributions')
        self.assertEqual(items, [{'bank_text': 'pos example shop', 'merchant': 'Local shop'}])

    def test_initial_library_uploads_before_download_and_pending_names_are_preserved(self):
        for index in range(30):
            self.seed(f'POS SHOP {index:02d}', f'Local shop {index:02d}')
        self.sync.configure(True, CONSENT_VERSION)
        self.assertTrue(self.sync.sync_once())
        self.assertEqual(self.sync.status()['upload_remaining'], 5)
        self.assertFalse(any(endpoint.startswith('/v1/rules') or endpoint == '/v1/resolutions' for _, endpoint, _ in self.calls))
        self.assertTrue(self.sync.sync_once())
        self.assertEqual(self.sync.status()['awaiting_review'], 30)
        with connect(self.path) as db:
            self.assertEqual(db.execute("SELECT merchant FROM merchant_rules WHERE title='pos shop 00'").fetchone()[0], 'Local shop 00')

    def test_canonical_name_returns_to_app_and_opt_out_disables_lookup(self):
        item = self.seed()
        self.sync.configure(True, CONSENT_VERSION)
        self.assertTrue(self.sync.sync_once())
        self.canonical = 'Approved shop'
        self.assertTrue(self.sync.sync_once())
        with connect(self.path) as db:
            self.assertEqual(db.execute('SELECT merchant FROM transactions WHERE id=?', (item,)).fetchone()[0], self.canonical)
            self.assertEqual(db.execute("SELECT merchant FROM merchant_rules WHERE title='pos example shop'").fetchone()[0], self.canonical)
        self.sync.configure(False, CONSENT_VERSION)
        self.assertFalse(self.sync.status()['enabled'])
        self.assertEqual(self.sync.status()['cached'], 0)


if __name__ == '__main__':
    unittest.main()
