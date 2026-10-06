import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from banking import import_account, normalize_transactions
from db import connect


class Provider:
    configured = True


def month_after(offset):
    today = date.today()
    number = today.year * 12 + today.month - 1 + offset
    year, month0 = divmod(number, 12)
    return f'{year:04d}-{month0 + 1:02d}'


class InsightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({'TESTING': True, 'DATA_DIR': self.temp.name, 'PROVIDER': Provider()})
        self.client = self.app.test_client()
        html = self.client.get('/').text
        self.headers = {'X-CSRF-Token': re.search(r'name="csrf-token" content="([^"]+)"', html)[1]}
        self.db = self.app.extensions['db_path']

    def tearDown(self):
        self.temp.cleanup()

    def test_merchant_rule_is_exact_remembered_and_used_by_analysis(self):
        with connect(self.db) as db:
            row = db.execute(
                "SELECT id,description FROM transactions WHERE account_id='demo-daily' AND description='REMA 1000' LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
        response = self.client.patch(
            f"/api/transactions/{row['id']}/merchant",
            json={'merchant': "McDonald's", 'remember': True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['rule_saved'])
        with connect(self.db) as db:
            self.assertGreater(db.execute(
                """SELECT count(*) FROM transactions t JOIN accounts a ON a.id=t.account_id
                   WHERE a.source='demo' AND transaction_title(t.description)=transaction_title(?) AND t.merchant=?""",
                (row['description'], "McDonald's")).fetchone()[0], 1)

        raw = {
            'status': 'BOOK', 'booking_date': date.today().isoformat(), 'credit_debit_indicator': 'DBIT',
            'transaction_amount': {'amount': '40.00', 'currency': 'DKK'},
            'remittance_information': ['REMA 1000'], 'entry_reference': 'merchant-rule-new',
        }
        similar = {
            **raw, 'remittance_information': ['REMA 1000 Næstved'], 'entry_reference': 'merchant-rule-similar',
        }
        import_account(self.db, 'demo-daily', [raw, similar], [])
        exact_id = normalize_transactions([raw])[0][0]
        similar_id = normalize_transactions([similar])[0][0]
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT merchant FROM transactions WHERE external_id=?', (exact_id,)).fetchone()[0], "McDonald's")
            self.assertIsNone(db.execute('SELECT merchant FROM transactions WHERE external_id=?', (similar_id,)).fetchone()[0])

        search = self.client.get("/api/transactions?source=demo&q=McDonald").json
        self.assertGreater(search['total'], 0)
        self.assertTrue(all(item['merchant'] == "McDonald's" for item in search['items']))

    def test_merchant_autocomplete_reuses_canonical_name_and_exposes_manual_completion(self):
        with connect(self.db) as db:
            rows = db.execute("""SELECT id,description FROM transactions
                                 WHERE account_id='demo-daily' AND amount<0
                                 ORDER BY id LIMIT 2""").fetchall()
        first, second = rows
        self.assertEqual(self.client.patch(
            f"/api/transactions/{first['id']}/merchant",
            json={'merchant': "McDonald's", 'remember': True},
            headers=self.headers).status_code, 200)

        merchants = self.client.get('/api/merchants?source=demo').json['items']
        self.assertIn("McDonald's", [item['name'] for item in merchants])

        response = self.client.patch(
            f"/api/transactions/{second['id']}/merchant",
            json={'merchant': 'mcdonalds', 'remember': False},
            headers=self.headers)
        self.assertEqual(response.status_code, 200)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT merchant FROM transactions WHERE id=?', (second['id'],)).fetchone()[0], "McDonald's")

        item = next(item for item in self.client.get('/api/transactions?source=demo').json['items']
                    if item['id'] == second['id'])
        self.assertEqual(item['category_manual'], 0)
        self.assertEqual(item['merchant'], "McDonald's")
        self.client.patch(f"/api/transactions/{second['id']}",
                          json={'category': item['category']}, headers=self.headers)
        item = next(item for item in self.client.get('/api/transactions?source=demo').json['items']
                    if item['id'] == second['id'])
        self.assertEqual(item['category_manual'], 1)

    def test_merchant_library_can_delete_rule_or_entire_merchant(self):
        with connect(self.db) as db:
            rows = db.execute("""SELECT id,description FROM transactions
                                 WHERE account_id='demo-daily' AND amount<0
                                 ORDER BY id LIMIT 2""").fetchall()
        first, second = rows
        for row in (first, second):
            response=self.client.patch(
                f"/api/transactions/{row['id']}/merchant",
                json={'merchant':'Test Merchant','remember':True},headers=self.headers)
            self.assertEqual(response.status_code,200)

        library=self.client.get('/api/merchant-library?source=demo').json['items']
        merchant=next(item for item in library if item['name']=='Test Merchant')
        self.assertEqual(merchant['rule_count'],2)
        first_rule=merchant['rules'][0]
        response=self.client.delete('/api/merchant-library?source=demo',
                                    json={'title':first_rule['title']},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['cleared_transactions'],0)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT merchant FROM transactions WHERE id=?',(first['id'],)).fetchone()[0],'Test Merchant')
        merchant=next(item for item in self.client.get('/api/merchant-library?source=demo').json['items']
                      if item['name']=='Test Merchant')
        self.assertEqual(merchant['rule_count'],1)

        response=self.client.delete('/api/merchant-library?source=demo',
                                    json={'merchant':'Test Merchant'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertGreaterEqual(response.json['cleared_transactions'],2)
        self.assertFalse(any(item['name']=='Test Merchant'
                             for item in self.client.get('/api/merchant-library?source=demo').json['items']))
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM merchant_rules WHERE source='demo' AND merchant='Test Merchant'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM transactions WHERE merchant='Test Merchant'").fetchone()[0],0)

    def test_goal_analysis_flags_unrealistic_goal_and_tracks_featured_progress(self):
        body = {
            'name': 'Stor drøm',
            'target_amount': '1000000',
            'saved_amount': '10000',
            'deadline': month_after(1),
            'featured': True,
        }
        response = self.client.post('/api/savings-goals?source=demo', json=body, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        goal_id = response.json['id']

        result = self.client.get('/api/savings-goals?source=demo').json
        goal = next(item for item in result['items'] if item['id'] == goal_id)
        self.assertEqual(goal['saved_amount'], 1000000)
        self.assertTrue(goal['featured'])
        self.assertEqual(goal['analysis']['status'], 'not_realistic')
        self.assertGreater(goal['analysis']['required_monthly'], goal['analysis']['average_available'])
        self.assertGreater(result['profile']['months_analyzed'], 0)
        self.assertTrue(result['profile']['categories'])
        self.assertTrue(result['profile']['merchants'])
        self.assertTrue(goal['analysis']['suggestions'])

        dashboard = self.client.get('/api/dashboard?source=demo').json
        self.assertEqual(dashboard['featured_goal']['id'], goal_id)
        self.assertEqual(dashboard['featured_goal']['analysis']['status'], 'not_realistic')

        second = self.client.post('/api/savings-goals?source=demo', json={
            'name': 'Mindre mål', 'target_amount': '1000', 'saved_amount': '100',
            'deadline': month_after(6), 'featured': True,
        }, headers=self.headers).json['id']
        items = self.client.get('/api/savings-goals?source=demo').json['items']
        self.assertEqual([item['id'] for item in items if item['featured']], [second])
        self.assertEqual(self.client.get('/api/dashboard?source=demo').json['featured_goal']['id'], second)

    def test_goal_rejects_saved_amount_above_target(self):
        response = self.client.post('/api/savings-goals', json={
            'name': 'Fejl', 'target_amount': '1000', 'saved_amount': '1001',
            'deadline': month_after(3), 'featured': False,
        }, headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
