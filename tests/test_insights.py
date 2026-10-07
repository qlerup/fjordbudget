import re
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from banking import import_account, normalize_transactions
from db import connect, transaction_rule_title


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

    def test_numeric_bank_text_variants_share_merchant_but_ambiguous_short_mcd_does_not(self):
        self.assertEqual(transaction_rule_title('MCD 06151 MCDRONNEDE'),
                         transaction_rule_title('MCD 06150 MCDRONNEDE'))
        self.assertNotEqual(transaction_rule_title('MCD 06151'),
                            transaction_rule_title('MCD 06150'))
        self.assertNotEqual(transaction_rule_title('MCD 06151 MCDRONNEDE'),
                            transaction_rule_title('MCD 06151 KORTKØB'))

        with connect(self.db) as db:
            db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                          VALUES ('demo-daily','mcd-a',?,'MCD 06151 MCDRONNEDE',-3200,'DKK','Mad & indkøb')""",
                       (date.today().isoformat(),))
            db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                          VALUES ('demo-daily','mcd-b',?,'MCD 06150 MCDRONNEDE',-12700,'DKK','Andet')""",
                       (date.today().isoformat(),))
            first=db.execute("SELECT id FROM transactions WHERE external_id='mcd-a'").fetchone()[0]
        response=self.client.patch(f'/api/transactions/{first}/merchant',
                                   json={'merchant':"McDonald's",'remember':True},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['updated'],2)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT merchant FROM transactions WHERE external_id='mcd-b'").fetchone()[0],"McDonald's")

    def test_fixed_merchant_is_excluded_from_savings_opportunities(self):
        with connect(self.db) as db:
            row = db.execute(
                "SELECT id FROM transactions WHERE account_id='demo-daily' AND description='ARKET' ORDER BY booked_on DESC LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)

        response = self.client.patch(
            f"/api/transactions/{row['id']}/merchant",
            json={'merchant': 'Børnehave', 'remember': True, 'adjustable': False},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)

        merchants = self.client.get('/api/merchants?source=demo').json['items']
        merchant = next(item for item in merchants if item['name'] == 'Børnehave')
        self.assertFalse(merchant['adjustable'])

        library = self.client.get('/api/merchant-library?source=demo').json['items']
        merchant = next(item for item in library if item['name'] == 'Børnehave')
        self.assertFalse(merchant['adjustable'])

        profile = self.client.get('/api/savings-goals?source=demo').json['profile']
        self.assertFalse(any(
            item['type'] == 'merchant' and item['name'] == 'Børnehave'
            for item in profile['opportunities']
        ))

        response = self.client.patch(
            f"/api/transactions/{row['id']}/merchant",
            json={'merchant': 'Børnehave', 'remember': True, 'adjustable': True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        profile = self.client.get('/api/savings-goals?source=demo').json['profile']
        self.assertTrue(any(
            item['type'] == 'merchant' and item['name'] == 'Børnehave'
            for item in profile['opportunities']
        ))

    def test_savings_analysis_defaults_to_last_month_and_accepts_custom_dates(self):
        today = date.today()
        first_this_month = today.replace(day=1)
        previous_end = first_this_month - timedelta(days=1)
        previous_start = previous_end.replace(day=1)

        response = self.client.get('/api/savings-goals?source=demo')
        self.assertEqual(response.status_code, 200)
        profile = response.json['profile']
        self.assertEqual(profile['period_start'], previous_start.isoformat())
        self.assertEqual(profile['period_end'], previous_end.isoformat())

        custom = self.client.get(
            f'/api/savings-goals?source=demo&from={previous_start.isoformat()}&to={previous_start.isoformat()}'
        )
        self.assertEqual(custom.status_code, 200)
        self.assertEqual(custom.json['profile']['period_start'], previous_start.isoformat())
        self.assertEqual(custom.json['profile']['period_end'], previous_start.isoformat())
        self.assertEqual(custom.json['profile']['period_days'], 1)

        invalid = self.client.get(
            f'/api/savings-goals?source=demo&from={previous_end.isoformat()}&to={previous_start.isoformat()}'
        )
        self.assertEqual(invalid.status_code, 400)

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

    def test_goal_can_follow_live_account_balance_and_restore_manual_amount(self):
        body = {
            'name': 'Konto-mål',
            'target_amount': '100000',
            'saved_amount': '500',
            'account_id': 'demo-save',
            'deadline': month_after(12),
            'featured': True,
        }
        response=self.client.post('/api/savings-goals?source=demo',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200)
        goal_id=response.json['id']

        result=self.client.get('/api/savings-goals?source=demo').json
        self.assertIn('demo-save',[account['id'] for account in result['accounts']])
        goal=next(item for item in result['items'] if item['id']==goal_id)
        self.assertTrue(goal['uses_live_balance'])
        self.assertEqual(goal['account_id'],'demo-save')
        self.assertEqual(goal['account_name'],'Opsparing')
        self.assertEqual(goal['manual_saved_amount'],50000)
        self.assertEqual(goal['saved_amount'],6840000)

        with connect(self.db) as db:
            db.execute("UPDATE accounts SET balance=7000000 WHERE id='demo-save'")
        goal=next(item for item in self.client.get('/api/savings-goals?source=demo').json['items']
                  if item['id']==goal_id)
        self.assertEqual(goal['saved_amount'],7000000)
        self.assertEqual(self.client.get('/api/dashboard?source=demo').json['featured_goal']['saved_amount'],7000000)

        body['account_id']=None
        response=self.client.put(f'/api/savings-goals/{goal_id}?source=demo',json=body,headers=self.headers)
        self.assertEqual(response.status_code,200)
        goal=next(item for item in self.client.get('/api/savings-goals?source=demo').json['items']
                  if item['id']==goal_id)
        self.assertFalse(goal['uses_live_balance'])
        self.assertEqual(goal['saved_amount'],50000)

    def test_goal_rejects_saved_amount_above_target(self):
        response = self.client.post('/api/savings-goals', json={
            'name': 'Fejl', 'target_amount': '1000', 'saved_amount': '1001',
            'deadline': month_after(3), 'featured': False,
        }, headers=self.headers)
        self.assertEqual(response.status_code, 400)


if __name__ == '__main__':
    unittest.main()
