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

    def test_merchant_recognition_learns_from_two_confirmed_store_variants(self):
        labels = [
            ('learn-a', 'MCD 01140 REMA TROMPETVEJ'),
            ('learn-b', 'MCD 01141 REMA BOULEVARD'),
            ('learn-c', 'MCD 01142 REMA ØSTERGADE'),
            ('learn-netto', 'MCD 01999 NETTO HOLME OLS'),
        ]
        with connect(self.db) as db:
            for external, description in labels:
                db.execute(
                    '''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                       VALUES ('demo-daily',?,?,?,-37040,'DKK','Mad & indkøb')''',
                    (external, date.today().isoformat(), description))
            ids = {row['external_id']: row['id'] for row in db.execute(
                "SELECT id,external_id FROM transactions WHERE external_id LIKE 'learn-%'")}

        url = lambda key: f"/api/transactions/{ids[key]}/merchant"
        response = self.client.patch(url('learn-a'), json={
            'merchant': 'REMA 1000', 'remember': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        with connect(self.db) as db:
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='learn-c'").fetchone()[0])

        response = self.client.patch(url('learn-b'), json={
            'merchant': 'REMA 1000', 'remember': True}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(response.json['updated'], 2)
        with connect(self.db) as db:
            learned = db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE external_id='learn-c'").fetchone()
            self.assertEqual(tuple(learned), ('REMA 1000', 0))
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='learn-netto'").fetchone()[0])

        imported = [
            {'status': 'BOOK', 'booking_date': date.today().isoformat(),
             'credit_debit_indicator': 'DBIT',
             'transaction_amount': {'amount': '40.00', 'currency': 'DKK'},
             'remittance_information': [name], 'entry_reference': ref}
            for ref, name in [
                ('learning-new-rema', 'MCD 11444 REMA HAVNEGADE'),
                ('learning-new-netto', 'MCD 11445 NETTO HAVNEGADE')]]
        import_account(self.db, 'demo-daily', imported, [])
        with connect(self.db) as db:
            matched = db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE external_id='ref:learning-new-rema'").fetchone()
            self.assertEqual(tuple(matched), ('REMA 1000', 0))
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='ref:learning-new-netto'").fetchone()[0])
        # A personal correction wins even if a future sync recognizes the brand.
        corrected_id = ids['learn-c']
        self.assertEqual(self.client.patch(
            f'/api/transactions/{corrected_id}/merchant',
            json={'merchant': 'Specialbutik', 'remember': False},
            headers=self.headers).status_code, 200)
        with connect(self.db) as db:
            self.assertEqual(tuple(db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE id=?",
                (corrected_id,)).fetchone()), ('Specialbutik', 1))

    def test_merchant_recognition_handles_mobilepay_variants_without_guessing_people(self):
        with connect(self.db) as db:
            db.execute(
                '''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                   VALUES ('demo-daily','learning-person',?,'MobilePay Sofia Joya',-30000,'DKK','Andet')''',
                (date.today().isoformat(),))
            row_id = db.execute(
                "SELECT id FROM transactions WHERE external_id='learning-person'").fetchone()[0]
        self.assertEqual(self.client.patch(
            f'/api/transactions/{row_id}/merchant',
            json={'merchant': 'Sofia Joya', 'remember': True},
            headers=self.headers).status_code, 200)

        bank_rows = [
            {'status': 'BOOK', 'booking_date': date.today().isoformat(),
             'credit_debit_indicator': 'DBIT',
             'transaction_amount': {'amount': '100.00', 'currency': 'DKK'},
             'remittance_information': [name], 'entry_reference': ref}
            for ref, name in [('learning-mobilepay-same', 'MobilePay: Sofia-Joya'),
                              ('learning-mobilepay-other', 'MobilePay Sofia Nielsen'),
                              ('learning-mcd-numeric', 'MCD 06151')]]
        import_account(self.db, 'demo-daily', bank_rows, [])
        with connect(self.db) as db:
            self.assertEqual(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='ref:learning-mobilepay-same'").fetchone()[0],
                'Sofia Joya')
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='ref:learning-mobilepay-other'").fetchone()[0])
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='ref:learning-mcd-numeric'").fetchone()[0])

    def test_conflicting_merchant_learning_stops_brand_wide_guesses(self):
        labels = [
            ('learning-conflict-a', 'MCD 10001 REMA TROMPETVEJ', 'REMA 1000'),
            ('learning-conflict-b', 'MCD 10002 REMA BOULEVARD', 'REMA 1000'),
            ('learning-conflict-c', 'MCD 10003 REMA ØSTERGADE', 'Anden forhandler'),
        ]
        with connect(self.db) as db:
            for external, description, _ in labels:
                db.execute(
                    '''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                       VALUES ('demo-daily',?,?,?,-12000,'DKK','Andet')''',
                    (external, date.today().isoformat(), description))
            ids = {row['external_id']: row['id'] for row in db.execute(
                "SELECT id,external_id FROM transactions WHERE external_id LIKE 'learning-conflict-%'")}
        for external, _, merchant in labels:
            response = self.client.patch(
                f'/api/transactions/{ids[external]}/merchant',
                json={'merchant': merchant, 'remember': True}, headers=self.headers)
            self.assertEqual(response.status_code, 200)

        raw = {'status': 'BOOK', 'booking_date': date.today().isoformat(),
               'credit_debit_indicator': 'DBIT',
               'transaction_amount': {'amount': '120.00', 'currency': 'DKK'},
               'remittance_information': ['MCD 10004 REMA HAVNEGADE'],
               'entry_reference': 'learning-conflict-new'}
        import_account(self.db, 'demo-daily', [raw], [])
        with connect(self.db) as db:
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='ref:learning-conflict-new'").fetchone()[0])

    def test_repeated_historic_gejser_confirmations_recognize_new_locations(self):
        # Several manually labelled purchases at ONE branch are useful history:
        # two different remembered rule titles must not be required.
        historical = 'MCD 00000 GEjSER Næstved'
        unknown = 'MCD 00000 GEjSER Roskilde'
        with connect(self.db) as db:
            for i in range(3):
                db.execute(
                    '''INSERT INTO transactions(account_id,external_id,booked_on,description,
                                                amount,currency,category,merchant,merchant_manual)
                       VALUES ('demo-daily',?,?,?,-8626,'DKK','Andet','Gejser',1)''',
                    (f'gejser-confirmed-{i}', date.today().isoformat(), historical))
            db.execute(
                '''INSERT INTO transactions(account_id,external_id,booked_on,description,
                                            amount,currency,category)
                   VALUES ('demo-daily','gejser-unknown',?,?,-8626,'DKK','Andet')''',
                (date.today().isoformat(), unknown))
            db.execute(
                '''INSERT INTO transactions(account_id,external_id,booked_on,description,
                                            amount,currency,category)
                   VALUES ('demo-daily','gejser-other',?,'MCD 00000 GEJSERBUTIK Roskilde',
                           -8626,'DKK','Andet')''',
                (date.today().isoformat(),))
            db.execute("DELETE FROM app_migrations WHERE name='learn-merchant-history-v2'")

        # Existing unrecognized transactions are backfilled on app update/start.
        create_app({'TESTING': True, 'DATA_DIR': self.temp.name, 'PROVIDER': Provider()})
        with connect(self.db) as db:
            learned = db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE external_id='gejser-unknown'"
            ).fetchone()
            self.assertEqual(tuple(learned), ('Gejser', 0))
            self.assertIsNone(db.execute(
                "SELECT merchant FROM transactions WHERE external_id='gejser-other'"
            ).fetchone()[0])

        new = {'status': 'BOOK', 'booking_date': date.today().isoformat(),
               'credit_debit_indicator': 'DBIT',
               'transaction_amount': {'amount': '86.26', 'currency': 'DKK'},
               'remittance_information': ['MCD 00000 GEJSER Køge'],
               'entry_reference': 'gejser-future'}
        import_account(self.db, 'demo-daily', [new], [])
        with connect(self.db) as db:
            result = db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE external_id='ref:gejser-future'"
            ).fetchone()
            self.assertEqual(tuple(result), ('Gejser', 0))

    def test_manual_gejser_history_learns_without_remembering_single_rule(self):
        labels = ['MCD 00000 GEJSER Næstved'] * 3
        labels.append('MCD 00000 GEJSER Roskilde')
        with connect(self.db) as db:
            for i, text in enumerate(labels):
                db.execute(
                    '''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                       VALUES ('demo-daily',?,?,?,-4000,'DKK','Andet')''',
                    (f'gejser-manual-{i}', date.today().isoformat(), text))
            ids = [row[0] for row in db.execute(
                "SELECT id FROM transactions WHERE external_id LIKE 'gejser-manual-%' ORDER BY external_id")]
        for i in range(3):
            result = self.client.patch(
                f'/api/transactions/{ids[i]}/merchant',
                json={'merchant': 'Gejser', 'remember': False}, headers=self.headers)
            self.assertEqual(result.status_code, 200)
            with connect(self.db) as db:
                matched = db.execute(
                    "SELECT merchant FROM transactions WHERE id=?", (ids[3],)).fetchone()[0]
                self.assertEqual(matched, 'Gejser' if i == 2 else None)
        with connect(self.db) as db:
            self.assertEqual(db.execute(
                "SELECT COUNT(*) FROM merchant_rules WHERE source='demo' AND merchant='Gejser'"
            ).fetchone()[0], 0)
            self.assertEqual(db.execute(
                "SELECT merchant_manual FROM transactions WHERE id=?", (ids[3],)).fetchone()[0], 0)

    def test_existing_saved_merchant_examples_are_applied_on_upgrade(self):
        with connect(self.db) as db:
            for i, label in enumerate([
                'MCD 13110 LIDL NÆSTVED',
                'MCD 13111 LIDL SLAGELSE',
                'MCD 13112 LIDL RINGSTED',
            ]):
                db.execute(
                    '''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                       VALUES ('demo-daily',?,?,?,-12000,'DKK','Andet')''',
                    (f'learn-upgrade-{i}', date.today().isoformat(), label))
            for label in ['MCD 13110 LIDL NÆSTVED', 'MCD 13111 LIDL SLAGELSE']:
                db.execute(
                    "INSERT INTO merchant_rules(source,title,merchant) VALUES ('demo',?,?)",
                    (transaction_rule_title(label), 'Lidl'))
            # Simulate an installation that predates the new one-time backfill.
            db.execute("DELETE FROM app_migrations WHERE name='learn-merchant-variants'")
            db.execute(
                '''UPDATE transactions SET merchant='Personligt valg',merchant_manual=1
                   WHERE external_id='learn-upgrade-0' ''')
        # Reinitialization must fill missing matches but respect manual corrections.
        create_app({'TESTING': True, 'DATA_DIR': self.temp.name, 'PROVIDER': Provider()})
        with connect(self.db) as db:
            matched = db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE external_id='learn-upgrade-2'").fetchone()
            self.assertEqual(tuple(matched), ('Lidl', 0))
            preserved = db.execute(
                "SELECT merchant,merchant_manual FROM transactions WHERE external_id='learn-upgrade-0'").fetchone()
            self.assertEqual(tuple(preserved), ('Personligt valg', 1))
            seeded = db.execute(
                "SELECT merchant FROM transactions WHERE account_id='demo-daily' AND description='Netto' LIMIT 1").fetchone()
            self.assertIsNotNone(seeded)
            self.assertEqual(seeded[0], 'Netto')

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
            '/api/merchant-library?source=demo',
            json={'merchant': 'Børnehave', 'adjustable': True},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['adjustable'])
        profile = self.client.get('/api/savings-goals?source=demo').json['profile']
        self.assertTrue(any(
            item['type'] == 'merchant' and item['name'] == 'Børnehave'
            for item in profile['opportunities']
        ))

    def test_savings_opportunities_are_merchant_only(self):
        profile = self.client.get('/api/savings-goals?source=demo').json['profile']
        self.assertTrue(profile['opportunities'])
        self.assertTrue(all(item['type'] == 'merchant' for item in profile['opportunities']))
        self.assertFalse(any(item['name'].startswith('Øvrigt i ') for item in profile['opportunities']))

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
            'analysis_account_ids': ['demo-daily', 'demo-bills'],
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

    def test_savings_analysis_uses_only_explicit_goal_accounts(self):
        url = '/api/savings-goals?source=demo'
        base = {'target_amount': '1000000', 'saved_amount': '0',
                'deadline': month_after(2)}
        first = self.client.post(url, json={**base, 'name': 'Daglig konto',
            'analysis_account_ids': ['demo-daily']}, headers=self.headers)
        second = self.client.post(url, json={**base, 'name': 'Budgetkonto',
            'analysis_account_ids': ['demo-bills']}, headers=self.headers)
        third = self.client.post(url, json={**base, 'name': 'Uden konti'},
            headers=self.headers)
        self.assertEqual([first.status_code, second.status_code, third.status_code], [200, 200, 200])

        def goals(client=self.client):
            return {goal['name']: goal for goal in client.get(url).json['items']}

        items = goals()
        daily = items['Daglig konto']
        bills = items['Budgetkonto']
        empty = items['Uden konti']
        self.assertEqual(daily['analysis_account_ids'], ['demo-daily'])
        self.assertEqual(bills['analysis_account_ids'], ['demo-bills'])
        self.assertEqual(empty['analysis_account_ids'], [])
        self.assertEqual(empty['analysis']['status'], 'insufficient_data')
        self.assertEqual(empty['analysis']['suggestions'], [])
        self.assertEqual(empty['analysis']['months_analyzed'], 0)
        self.assertGreater(daily['analysis']['average_income'], 0)
        self.assertEqual(bills['analysis']['average_income'], 0)
        daily_merchants = {item['name'] for item in daily['analysis']['suggestions']}
        bills_merchants = {item['name'] for item in bills['analysis']['suggestions']}
        self.assertTrue(daily_merchants)
        self.assertTrue(bills_merchants)
        self.assertTrue(daily_merchants.isdisjoint(bills_merchants))

        changed = self.client.put(f"/api/savings-goals/{first.json['id']}?source=demo",
            json={**base, 'name': 'Daglig konto',
                  'analysis_account_ids': ['demo-bills', 'demo-daily']},
            headers=self.headers)
        self.assertEqual(changed.status_code, 200)
        updated = goals()['Daglig konto']
        self.assertEqual(updated['analysis_account_ids'], ['demo-bills', 'demo-daily'])
        self.assertGreater(updated['analysis']['average_income'], 0)

        reopened = create_app({'TESTING': True, 'DATA_DIR': self.temp.name,
                               'PROVIDER': Provider()}).test_client()
        self.assertEqual(goals(reopened)['Daglig konto']['analysis_account_ids'],
                         ['demo-bills', 'demo-daily'])

        cleared = self.client.put(f"/api/savings-goals/{first.json['id']}?source=demo",
            json={**base, 'name': 'Daglig konto', 'analysis_account_ids': []},
            headers=self.headers)
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(goals()['Daglig konto']['analysis']['months_analyzed'], 0)
        self.assertEqual(self.client.delete(
            f"/api/savings-goals/{first.json['id']}?source=demo",
            headers=self.headers).status_code, 200)
        with connect(self.db) as db:
            self.assertEqual(db.execute(
                'SELECT COUNT(*) FROM savings_goal_analysis_accounts WHERE goal_id=?',
                (first.json['id'],)).fetchone()[0], 0)

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
