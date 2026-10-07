import hashlib
import re
import sys
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app
from banking import BankError, EnableBanking, account_name, import_account, normalize_transactions, sync_all
from db import CATEGORIES, cents, connect


class Provider:
    configured = True
    def __init__(self):
        self.calls = []
        self.history_items = []

    def banks(self):
        return [{'name':'Test Bank','country':'DK','psu_types':['personal']}]

    def transactions(self, uid, start=None, end=None, strategy='default'):
        self.calls.append(('TRANSACTIONS', uid, {'start':start, 'end':end, 'strategy':strategy}))
        return list(self.history_items) if strategy == 'longest' else []

    def request(self, method, path, **kwargs):
        self.calls.append((method,path,kwargs))
        if path == '/auth':
            return {'url':'https://auth.enablebanking.com/ais/start?sessionid=test'}
        if path == '/sessions':
            return {'session_id':'secret-session-token','access':{'valid_until':(datetime.now(timezone.utc)+timedelta(days=90)).isoformat()},
                    'accounts':[{'uid':'remote-account','identification_hash':'stable-account','account_id':{'iban':'DK0000001234'},'currency':'DKK','name':'Min konto'}]}
        return {'message':'OK'}


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.provider = Provider()
        self.app = create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider})
        self.client = self.app.test_client()
        html = self.client.get('/').text
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"',html)[1]
        self.headers = {'X-CSRF-Token':self.csrf}
        self.month = date.today().strftime('%Y-%m')
        self.db = self.app.extensions['db_path']

    def tearDown(self):
        self.temp.cleanup()

    def select_pending_accounts(self, selected=True):
        items=self.client.get('/api/accounts/manage?pending=1').json['items']
        if items:
            response=self.client.put('/api/accounts/manage',
                json={'included':{item['id']:selected for item in items}},headers=self.headers)
            self.assertEqual(response.status_code,200)
        return items

    def test_demo_and_real_are_separate_and_transfers_do_not_inflate_totals(self):
        demo=self.client.get('/api/dashboard?source=demo').json
        live=self.client.get('/api/dashboard?source=live').json
        self.assertEqual(len(demo['accounts']),3)
        self.assertEqual(demo['income'],3250000)
        self.assertEqual(live['income'],0)
        self.assertEqual(live['accounts'],[])
        euro=self.client.get('/api/dashboard?source=demo&currency=EUR').json
        self.assertEqual((euro['balance'],euro['income'],euro['expenses']),(0,0,0))

    def test_budget_category_lifecycle_preserves_transactions(self):
        endpoint = '/api/budget-categories'
        self.assertEqual(self.client.post(endpoint, json={'name':'Ferie'}).status_code, 403)
        for name in ['', 'x'*61, 'bad\nname']:
            self.assertEqual(self.client.post(endpoint, json={'name':name}, headers=self.headers).status_code, 400)
        self.assertEqual(self.client.post(endpoint, json={'name':'Ferie'}, headers=self.headers).status_code, 200)
        self.assertEqual(self.client.post(endpoint, json={'name':' ferie '}, headers=self.headers).status_code, 400)
        with connect(self.db) as db:
            before = [tuple(r) for r in db.execute('SELECT * FROM transactions')]
            db.execute("UPDATE categories SET budget_category='Ferie' WHERE name='Transport'")
            for source in ['demo','live']:
                db.execute('INSERT INTO budgets VALUES (?,?,?,?,?)', (source,self.month,'DKK','Ferie',12300))
        result = self.client.delete(endpoint, json={'name':'Ferie'}, headers=self.headers)
        self.assertEqual(result.status_code, 200)
        with connect(self.db) as db:
            self.assertEqual(before, [tuple(r) for r in db.execute('SELECT * FROM transactions')])
            self.assertIsNone(db.execute("SELECT budget_category FROM categories WHERE name='Transport'").fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budgets WHERE category='Ferie'").fetchone()[0], 0)
        self.assertEqual(self.client.delete(endpoint, json={'name':'Ferie'}, headers=self.headers).status_code, 400)
        self.client.delete(endpoint, json={'name':'Transport'}, headers=self.headers)
        create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider})
        self.assertNotIn('Transport', [c['name'] for c in self.client.get('/api/categories').json['budget_categories']])

    def test_savings_goals_validation_scope_persistence_and_delete(self):
        endpoint='/api/savings-goals'
        body={'name':'Ferie', 'target_amount':'12345.67', 'deadline':'2027-06'}
        self.assertEqual(self.client.post(endpoint,json=body).status_code,403)
        for field,value in [('name',''),('name','x'*81),('name','bad\nname'),('target_amount','0'),('target_amount','-1'),('target_amount','NaN'),('target_amount','0.001'),('deadline','2027-02-30'),('deadline','2027-13'),('deadline','2027-00'),('deadline','0000-01'),('deadline',None)]:
            with self.subTest(field=field,value=value):
                self.assertEqual(self.client.post(endpoint,json={**body,field:value},headers=self.headers).status_code,400)
        result=self.client.post(endpoint+'?source=live',json=body,headers=self.headers)
        self.assertEqual(result.status_code,200)
        goal_id=result.json['id']
        self.assertEqual(self.client.get(endpoint).json['items'],[])
        self.assertEqual(self.client.get(endpoint+'?source=live&currency=EUR').json['items'],[])
        item=self.client.get(endpoint+'?source=live&month=2028-01').json['items'][0]
        self.assertEqual(item['target_amount'],1234567)
        self.assertEqual(item['deadline'],'2027-06')
        with connect(self.db) as db:
            db.execute('UPDATE savings_goals SET deadline=? WHERE id=?', ('2027-06-25',goal_id))
        self.assertEqual(self.client.get(endpoint+'?source=live').json['items'][0]['deadline'],'2027-06')
        self.assertEqual(self.client.delete(f'{endpoint}/{goal_id}',headers=self.headers).status_code,404)
        self.assertEqual(self.client.put(f'{endpoint}/{goal_id}?source=live',json={**body,'name':'Bil','target_amount':'20000'},headers=self.headers).status_code,200)
        restarted=create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider}).test_client()
        self.assertEqual(restarted.get(endpoint+'?source=live').json['items'][0]['name'],'Bil')
        self.assertEqual(self.client.delete(f'{endpoint}/{goal_id}?source=live',headers=self.headers).status_code,200)
        self.assertEqual(self.client.get(endpoint+'?source=live').json['items'],[])

    def test_demo_availability_depends_on_bank_connection_not_accounts(self):
        self.assertFalse(self.client.get('/api/config').json['has_bank_connections'])
        with connect(self.db) as db:
            db.execute("INSERT INTO connections VALUES ('bank-test','Test Bank','unused','2027-12-31','2026-10-02')")
        self.assertTrue(self.client.get('/api/config').json['has_bank_connections'])
        for source in ['live','demo']:
            self.assertTrue(self.client.get('/api/dashboard?source='+source).json['has_bank_connections'])
        with connect(self.db) as db:
            db.execute("DELETE FROM connections WHERE id='bank-test'")
        self.assertFalse(self.client.get('/api/config').json['has_bank_connections'])

    def test_amounts_are_exact_and_invalid_precision_rejected(self):
        self.assertEqual(cents('0.29'),29)
        self.assertEqual(cents('-573.20'),-57320)
        for invalid in ['0.001','NaN','Infinity','hello']:
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                cents(invalid)

    def test_budget_suggestion_uses_previous_twelve_complete_months(self):
        with connect(self.db) as db:
            db.execute("""INSERT INTO accounts(id,source,name,bank,last4,currency,included,selection_pending)
                          VALUES ('budget-suggest-live','live','Budget test','Bank','1111','DKK',1,0)""")
            db.execute("""INSERT OR IGNORE INTO categories(name,color,protected,requires_merchant,budget_category)
                          VALUES ('Ikke budgetteret','#999999',0,1,NULL)""")
            start_ordinal = 2025 * 12 + 10 - 1
            for i in range(12):
                year, month0 = divmod(start_ordinal + i, 12)
                booked = f'{year:04d}-{month0+1:02d}-15'
                for suffix, category, amount in [
                    ('food','Mad & indkøb',-100000),
                    ('home','Bolig',-200000),
                    ('unmapped','Ikke budgetteret',-30000),
                    ('transfer','Overførsler',-900000),
                ]:
                    db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                                  VALUES ('budget-suggest-live',?,?,?,?, 'DKK',?)""",
                               (f'{suffix}-{i}',booked,suffix,amount,category))
            db.execute("""INSERT INTO budgets(source,month,currency,category,amount)
                          VALUES ('live','2026-10','DKK','Mad & indkøb',500000)""")

        response=self.client.get('/api/budgets/suggestion?source=live&month=2026-10&currency=DKK')
        self.assertEqual(response.status_code,200)
        result=response.json
        self.assertEqual(result['period_start'],'2025-10-01')
        self.assertEqual(result['period_end'],'2026-09-30')
        self.assertEqual(result['months_analyzed'],12)
        self.assertEqual(len(result['months']),12)
        food=next(item for item in result['items'] if item['name']=='Mad & indkøb')
        home=next(item for item in result['items'] if item['name']=='Bolig')
        self.assertEqual(food['year_total'],1200000)
        self.assertEqual(food['monthly_average'],100000)
        self.assertEqual(food['suggested'],105000)
        self.assertEqual(food['current_budget'],500000)
        self.assertEqual(home['monthly_average'],200000)
        self.assertEqual(home['suggested'],210000)
        self.assertEqual(result['unmapped_total'],360000)
        self.assertEqual(result['unmapped_monthly_average'],30000)
        self.assertFalse(any(item['name']=='Overførsler' for item in result['items']))

    def test_budget_is_persistent_and_scoped_to_month_currency_and_mode(self):
        values={cat:'123.45' for cat in CATEGORIES[:8]}
        response=self.client.put('/api/budgets?source=live',json={'amounts':values},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['budget'],98760)
        self.assertNotEqual(self.client.get('/api/dashboard?source=demo').json['budget'],98760)
        reopened=create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider}).test_client()
        self.assertEqual(reopened.get('/api/dashboard?source=live').json['budget'],98760)
        self.assertEqual(reopened.get('/api/dashboard?source=live&currency=EUR').json['budget'],0)

    def test_csrf_and_host_protection(self):
        self.assertEqual(self.client.post('/api/bank/connect',json={'bank':'Test Bank'}).status_code,403)
        self.assertEqual(self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers={**self.headers,'Origin':'https://evil.example'}).status_code,403)
        self.assertEqual(self.client.get('/api/dashboard',headers={'Host':'evil.example'}).status_code,400)
        self.assertEqual(self.provider.calls,[])

    def test_read_only_transport_rejects_payments_even_with_credentials(self):
        provider=EnableBanking('unused','unused')
        for method,path in [('POST','/payments'),('POST','/accounts/x/transfers'),('PUT','/accounts/x')]:
            with self.subTest(path=path),patch('banking.requests.request') as request:
                with self.assertRaises(BankError):
                    provider.request(method,path)
                request.assert_not_called()

    def test_setup_names_are_atomic_and_preserve_existing_names(self):
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        self.client.get('/bank/callback?state='+state+'&code=abc')
        self.select_pending_accounts()
        aid=self.client.get('/api/dashboard?source=live').json['accounts'][0]['id']
        url='/api/accounts/setup'
        self.assertEqual(self.client.put(url,json={'names':{aid:'Food'}}).status_code,403)
        self.assertEqual(self.client.put(url,json={'names':{aid:'Food','missing':'Bad'}},headers=self.headers).status_code,400)
        self.assertIsNone(self.client.get('/api/dashboard?source=live').json['accounts'][0]['custom_name'])
        self.assertEqual(self.client.put(url,json={'names':{aid:'Food'}},headers=self.headers).status_code,200)
        self.client.put(url,json={'names':{aid:'Changed'}},headers=self.headers)
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['accounts'][0]['name'],'Food')

    def test_account_label_uses_description_then_product_never_holder(self):
        self.assertEqual(account_name({'name':'Holder','details':' Holiday ','product':'Savings'}),'Holiday')
        self.assertEqual(account_name({'name':'Holder','details':' ','product':'Savings'}),'Savings')
        self.assertEqual(account_name({'name':'Holder'}),'Bankkonto')

    def test_manual_sync_reports_completion_and_default_auto_interval(self):
        self.assertEqual(self.app.extensions['auto_sync_interval_seconds'], 1800)
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        self.client.get('/bank/callback?state='+state+'&code=abc')
        self.select_pending_accounts()
        self.provider.history_items=[{
            'status':'BOOK','booking_date':'2024-01-15','credit_debit_indicator':'DBIT',
            'transaction_amount':{'amount':'10.00','currency':'DKK'},
            'remittance_information':['Historisk køb'],'entry_reference':'historic',
        }]
        self.assertEqual(self.client.post('/api/sync',json={},headers=self.headers).status_code,202)
        deadline=time.time()+2
        while time.time()<deadline:
            status=self.client.get('/api/sync').json
            if not status['running']:
                break
            time.sleep(0.01)
        self.assertFalse(status['running'])
        self.assertFalse(status['error'])
        self.assertFalse(status['automatic'])
        self.assertIsNotNone(status['completed_at'])
        datetime.fromisoformat(status['completed_at'])
        self.assertEqual(status['history']['earliest_date'],'2024-01-15')
        self.assertEqual(status['history']['transactions'],1)
        longest_calls=[call for call in self.provider.calls
                       if call[0]=='TRANSACTIONS' and call[2]['strategy']=='longest']
        self.assertEqual(len(longest_calls),1)

        self.assertEqual(self.client.post('/api/sync',json={},headers=self.headers).status_code,202)
        deadline=time.time()+2
        while time.time()<deadline:
            status=self.client.get('/api/sync').json
            if not status['running']:
                break
            time.sleep(0.01)
        self.assertFalse(status['error'])
        self.assertIsNone(status['history'])
        recent_calls=[call for call in self.provider.calls
                      if call[0]=='TRANSACTIONS' and call[2]['strategy']=='default']
        self.assertTrue(recent_calls)

    def test_sync_refreshes_existing_account_name(self):
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        self.client.get('/bank/callback?state='+state+'&code=abc')
        self.select_pending_accounts()
        aid=self.client.get('/api/dashboard?source=live').json['accounts'][0]['id']
        endpoint='/api/accounts/'+aid+'/name'
        self.assertEqual(self.client.put(endpoint,json={'name':'Food'},headers=self.headers).status_code,200)
        with connect(self.db) as db:
            db.execute("UPDATE accounts SET name='Holder' WHERE source='live'")
        def response(method,path,**kwargs):
            return {'name':'Holder','product':'Savings'} if path.endswith('/details') else {'balances':[]}
        with patch.object(self.provider,'request',side_effect=response), patch.object(self.provider,'transactions',return_value=[],create=True):
            sync_all(self.db,self.provider,self.app.extensions['cipher'],lambda message:None)
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['accounts'][0]['name'],'Food')
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        self.client.get('/bank/callback?state='+state+'&code=again')
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['accounts'][0]['name'],'Food')
        self.assertEqual(self.client.put(endpoint,json={'name':'Bad'}).status_code,403)
        for name in [None,123,'x'*101,'line\nbreak']:
            self.assertEqual(self.client.put(endpoint,json={'name':name},headers=self.headers).status_code,400)
        self.client.put(endpoint,json={'name':''},headers=self.headers)
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['accounts'][0]['name'],'Bankkonto')

    def test_authorization_redirect_accepts_both_provider_hosts_only(self):
        for url in ['https://auth.enablebanking.com/ais/start?sessionid=test',
                    'https://tilisy.enablebanking.com/ais/start?sessionid=test']:
            with self.subTest(url=url), patch.object(self.provider, 'request', return_value={'url':url}):
                response=self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
                self.assertEqual(response.status_code,200)
                self.assertEqual(response.json['url'],url)
        for url in ['http://tilisy.enablebanking.com/start', 'https://evil.example/start',
                    'https://tilisy.enablebanking.com.evil.example/start',
                    'https://tilisy.enablebanking.com:8443/start',
                    'https://user@tilisy.enablebanking.com/start']:
            with self.subTest(url=url), patch.object(self.provider, 'request', return_value={'url':url}):
                response=self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
                self.assertEqual(response.status_code,502)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM oauth_states').fetchone()[0],2)

    def test_callback_is_browser_bound_single_use_and_preserves_account_on_reconnect(self):
        def start():
            self.assertEqual(self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers).status_code,200)
            payload=self.provider.calls[-1][2]['json']
            self.assertEqual(set(payload['access']),{'valid_until'})
            return payload['state']
        state=start()
        stranger=self.app.test_client()
        response=stranger.get('/bank/callback?state='+state+'&code=abc')
        self.assertIn('invalid',response.location)
        response=self.client.get('/bank/callback?state='+state+'&code=abc')
        self.assertIn('connected',response.location)
        pending=self.client.get('/api/accounts/manage?pending=1').json['items']
        self.assertEqual(len(pending),1)
        self.assertFalse(pending[0]['included'])
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['accounts'],[])
        self.select_pending_accounts()
        self.assertIn('invalid',self.client.get('/bank/callback?state='+state+'&code=abc').location)
        with connect(self.db) as db:
            self.assertNotIn('secret-session-token',db.execute('SELECT session_token FROM connections').fetchone()[0])
            self.assertNotIn('remote-account',db.execute("SELECT remote_id FROM accounts WHERE source='live'").fetchone()[0])
        state=start()
        self.client.get('/bank/callback?state='+state+'&code=def')
        self.assertEqual(len(self.client.get('/api/dashboard?source=live').json['accounts']),1)

    def test_expired_state_is_rejected_and_cancelled_callback_does_not_exchange_code(self):
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        with connect(self.db) as db:
            db.execute('UPDATE oauth_states SET expires=?',(time.time()-1,))
        self.assertIn('invalid',self.client.get('/bank/callback?state='+state+'&code=abc').location)
        self.assertFalse(any(path=='/sessions' for _,path,_ in self.provider.calls))

    def test_accounts_can_be_hidden_without_deleting_history(self):
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        self.client.get('/bank/callback?state='+state+'&code=abc')
        pending=self.client.get('/api/accounts/manage?pending=1').json['items']
        self.assertEqual(len(pending),1)
        aid=pending[0]['id']
        self.client.put('/api/accounts/manage',json={'included':{aid:True}},headers=self.headers)
        with connect(self.db) as db:
            db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                          VALUES (?,?,?,?,?,?,?)''',(aid,'visibility',self.month+'-01','Visible purchase',-12345,'DKK','Andet'))
        self.assertEqual(len(self.client.get('/api/dashboard?source=live').json['accounts']),1)
        self.assertEqual(self.client.get('/api/transactions?source=live').json['total'],1)
        self.client.put('/api/accounts/manage',json={'included':{aid:False}},headers=self.headers)
        self.assertEqual(self.client.get('/api/dashboard?source=live').json['accounts'],[])
        self.assertEqual(self.client.get('/api/transactions?source=live').json['total'],0)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM transactions WHERE external_id='visibility'").fetchone()[0],1)
        self.client.put('/api/accounts/manage',json={'included':{aid:True}},headers=self.headers)
        self.assertEqual(self.client.get('/api/transactions?source=live').json['total'],1)
    def test_search_pagination_and_account_filters(self):
        result=self.client.get('/api/transactions?source=demo&q=netto').json
        self.assertTrue(all('netto' in t['description'].lower() for t in result['items']))
        result=self.client.get('/api/transactions?source=demo&account=demo-save').json
        self.assertTrue(all(t['account']=='Opsparing' for t in result['items']))
        self.assertEqual(self.client.get('/api/transactions?source=live&account=demo-save').json['total'],0)
        first=self.client.get('/api/transactions?source=demo').json
        second=self.client.get('/api/transactions?source=demo&page=2').json
        self.assertFalse({t['id'] for t in first['items']} & {t['id'] for t in second['items']})
        self.assertEqual(self.client.get('/api/dashboard?month=2026-99').status_code,400)

    def test_sync_is_idempotent_preserves_identical_purchases_and_manual_categories(self):
        raw={'status':'BOOK','booking_date':self.month+'-01','credit_debit_indicator':'DBIT','transaction_amount':{'amount':'12.34','currency':'DKK'},'remittance_information':['Coffee']}
        data=[raw,dict(raw),{**raw,'entry_reference':'real-reference'}]
        balances=[{'balance_type':'CLBD','balance_amount':{'amount':'99.95','currency':'DKK'}}]
        import_account(self.db,'demo-daily',data,balances)
        with connect(self.db) as db:
            row=db.execute("SELECT id FROM transactions WHERE description='Coffee' LIMIT 1").fetchone()
        self.client.patch('/api/transactions/'+str(row['id']),json={'category':'Fritid'},headers=self.headers)
        import_account(self.db,'demo-daily',data,balances)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM transactions WHERE description='Coffee'").fetchone()[0],3)
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE id=?',(row['id'],)).fetchone()[0],'Fritid')
            self.assertEqual(db.execute("SELECT balance FROM accounts WHERE id='demo-daily'").fetchone()[0],9995)
        invalid=[{**raw,'booking_date':None,'value_date':None}]
        with self.assertRaises(BankError):
            import_account(self.db,'demo-daily',invalid,balances)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM transactions WHERE description='Coffee'").fetchone()[0],3)

    def test_incomplete_transaction_count_and_filter_follow_merchant_requirement(self):
        with connect(self.db) as db:
            db.execute("INSERT INTO accounts(id,source,name,bank,last4,currency,included,selection_pending) VALUES ('missing-live','live','Test','Bank','9999','DKK',1,0)")
            rows=[
                ('missing-one','Missing merchant',-1000,'DKK','Andet',None),
                ('complete-one','Complete merchant',-2000,'DKK','Andet','Shop'),
                ('transfer-one','Transfer',-3000,'DKK','Overførsler',None),
                ('income-one','Income',4000,'DKK','Andet',None),
            ]
            for ref,description,amount,currency,category,merchant in rows:
                db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category,merchant)
                              VALUES ('missing-live',?,?,?,?,?,?,?)""",
                           (ref,self.month+'-01',description,amount,currency,category,merchant))
        dashboard=self.client.get('/api/dashboard?source=live&month='+self.month).json
        self.assertEqual(dashboard['incomplete_transactions'],1)
        missing=self.client.get('/api/transactions?source=live&month='+self.month+'&missing=1&per_page=200').json
        self.assertEqual(missing['total'],1)
        self.assertEqual(missing['items'][0]['description'],'Missing merchant')
        all_rows=self.client.get('/api/transactions?source=live&month='+self.month).json
        self.assertEqual(all_rows['total'],4)

    def test_transaction_exposes_category_merchant_requirement(self):
        with connect(self.db) as db:
            tid=db.execute("SELECT id FROM transactions WHERE account_id='demo-daily' AND category='Indkomst' LIMIT 1").fetchone()[0]
            month=db.execute("SELECT substr(booked_on,1,7) FROM transactions WHERE id=?",(tid,)).fetchone()[0]
        items=self.client.get('/api/transactions?source=demo&month='+month).json['items']
        income=next(item for item in items if item['id']==tid)
        self.assertEqual(income['requires_merchant'],0)
        self.client.patch('/api/categories',json={'name':'Indkomst','requires_merchant':True},headers=self.headers)
        items=self.client.get('/api/transactions?source=demo&month='+month).json['items']
        self.assertEqual(next(item for item in items if item['id']==tid)['requires_merchant'],1)

    def test_category_rule_updates_history_and_future_imports_across_accounts(self):
        with connect(self.db) as db:
            db.execute("INSERT INTO accounts(id,source,name,bank,last4,currency) VALUES ('live-test','live','Account','Bank','1234','DKK')")
            for aid,ref,title,day in [('demo-daily','rule1','Løn overførsel','2024-01-01'),
                    ('demo-bills','rule2','  LØN   OVERFØRSEL ','2025-02-01'),
                    ('demo-save','rule3','Løn overførsel ekstra','2025-02-01'),
                    ('live-test','rule4','Løn overførsel','2025-02-01')]:
                db.execute('INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category) VALUES (?,?,?,?,10000,?,?)',
                           (aid,ref,day,title,'DKK','Andet'))
            tid=db.execute("SELECT id FROM transactions WHERE external_id='rule1'").fetchone()[0]
        response=self.client.patch('/api/transactions/'+str(tid),json={'category':'Indkomst'},headers=self.headers)
        self.assertEqual(response.json['updated'],2)
        create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider})
        raw={'status':'BOOK','booking_date':'2026-09-01','credit_debit_indicator':'CRDT',
             'transaction_amount':{'amount':'200','currency':'DKK'},'remittance_information':['løn overførsel'],'entry_reference':'rule-new'}
        import_account(self.db,'demo-save',[raw],[])
        imported_id=normalize_transactions([raw])[0][0]
        with connect(self.db) as db:
            rows=dict(db.execute("SELECT external_id,category FROM transactions WHERE external_id LIKE 'rule%'"))
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE external_id=?',(imported_id,)).fetchone()[0],'Indkomst')
        self.assertEqual(rows,{'rule1':'Indkomst','rule2':'Indkomst','rule3':'Andet','rule4':'Andet'})
        self.client.patch('/api/transactions/'+str(tid),json={'category':'Fritid'},headers=self.headers)
        import_account(self.db,'demo-save',[raw],[])
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE external_id=?',(imported_id,)).fetchone()[0],'Fritid')

    def test_numeric_bank_text_variants_share_category_and_future_import(self):
        with connect(self.db) as db:
            for ref,title in [('numeric-cat-a','MCD 06151 MCDRONNEDE'),
                              ('numeric-cat-b','MCD 06150 MCDRONNEDE')]:
                db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                              VALUES ('demo-daily',?,?,?,-3200,'DKK','Andet')""",
                           (ref,self.month+'-01',title))
            tid=db.execute("SELECT id FROM transactions WHERE external_id='numeric-cat-a'").fetchone()[0]
        response=self.client.patch('/api/transactions/'+str(tid),json={'category':'Mad & indkøb'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json['updated'],2)
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT category FROM transactions WHERE external_id='numeric-cat-b'").fetchone()[0],'Mad & indkøb')

        raw={'status':'BOOK','booking_date':self.month+'-02','credit_debit_indicator':'DBIT',
             'transaction_amount':{'amount':'45','currency':'DKK'},
             'remittance_information':['MCD 06149 MCDRONNEDE'],'entry_reference':'numeric-cat-future'}
        import_account(self.db,'demo-daily',[raw],[])
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT category FROM transactions WHERE external_id='ref:numeric-cat-future'").fetchone()[0],
                             'Mad & indkøb')

    def test_category_management_preserves_separate_budgets(self):
        url='/api/categories'
        self.assertEqual(self.client.post(url,json={'name':'Travel'}).status_code,403)
        self.assertEqual(self.client.post(url,json={'name':'Travel'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.post(url,json={'name':' TRAVEL '},headers=self.headers).status_code,400)
        categories=self.client.get(url).json['items']
        self.assertFalse(next(c for c in categories if c['name']=='Indkomst')['requires_merchant'])
        self.assertFalse(next(c for c in categories if c['name']=='Overførsler')['requires_merchant'])
        self.assertTrue(next(c for c in categories if c['name']=='Travel')['requires_merchant'])
        self.assertEqual(self.client.patch(url,json={'name':'Travel','requires_merchant':False},headers=self.headers).status_code,200)
        self.assertFalse(next(c for c in self.client.get(url).json['items'] if c['name']=='Travel')['requires_merchant'])
        self.assertEqual(self.client.patch(url,json={'name':'Travel','requires_merchant':'no'},headers=self.headers).status_code,400)
        self.assertIn('Travel',self.client.get('/api/config').json['categories'])
        self.assertNotIn('Travel',[c['name'] for c in self.client.get('/api/dashboard').json['categories']])
        with connect(self.db) as db:
            db.execute("INSERT INTO budget_categories VALUES ('Travel','#809087')")
            tid=db.execute("SELECT id FROM transactions WHERE account_id='demo-daily' LIMIT 1").fetchone()[0]
            db.execute("INSERT INTO budgets VALUES ('live','2025-01','DKK','Travel',12300)")
            db.execute("INSERT INTO budgets VALUES ('live','2025-01','DKK','Andet',1000)")
        self.assertEqual(self.client.patch('/api/transactions/'+str(tid),json={'category':'Travel'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.delete(url,json={'name':'Travel','replacement':'Andet'},headers=self.headers).status_code,200)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE id=?',(tid,)).fetchone()[0],'Andet')
            self.assertEqual(db.execute("SELECT count(*) FROM category_rules WHERE category='Travel'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT amount FROM budgets WHERE source='live' AND month='2025-01' AND category='Andet'").fetchone()[0],1000)
            self.assertEqual(db.execute("SELECT amount FROM budgets WHERE source='live' AND month='2025-01' AND category='Travel'").fetchone()[0],12300)
        self.assertEqual(self.client.delete(url,json={'name':'Andet','replacement':'Bolig'},headers=self.headers).status_code,400)
        self.client.delete(url,json={'name':'Shopping','replacement':'Andet'},headers=self.headers)
        create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider})
        self.assertNotIn('Shopping',self.client.get('/api/config').json['categories'])
        raw={'status':'BOOK','booking_date':'2026-09-01','credit_debit_indicator':'DBIT','transaction_amount':{'amount':'12','currency':'DKK'},'remittance_information':['Matas'],'entry_reference':'deleted-default'}
        import_account(self.db,'demo-daily',[raw],[])
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE external_id=?',(normalize_transactions([raw])[0][0],)).fetchone()[0],'Andet')

    def test_custom_category_can_be_renamed_with_transactions_and_rules(self):
        url='/api/categories'
        self.assertEqual(self.client.post(url,json={'name':'Old name'},headers=self.headers).status_code,200)
        self.client.patch(url,json={'name':'Old name','requires_merchant':False},headers=self.headers)
        with connect(self.db) as db:
            db.execute("""INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category)
                          VALUES ('demo-daily','rename-category',?,'Rename purchase',-1000,'DKK','Old name')""",
                       (self.month+'-01',))
            db.execute("INSERT INTO category_rules(source,title,category) VALUES ('demo','rename purchase','Old name')")
        response=self.client.patch(url,json={'name':'Old name','new_name':'New name'},headers=self.headers)
        self.assertEqual(response.status_code,200)
        items=response.json['items']
        renamed=next(c for c in items if c['name']=='New name')
        self.assertFalse(renamed['requires_merchant'])
        self.assertNotIn('Old name',[c['name'] for c in items])
        with connect(self.db) as db:
            self.assertEqual(db.execute("SELECT category FROM transactions WHERE external_id='rename-category'").fetchone()[0],'New name')
            self.assertEqual(db.execute("SELECT category FROM category_rules WHERE title='rename purchase'").fetchone()[0],'New name')
        self.assertIn('New name',self.client.get('/api/config').json['categories'])
        self.assertEqual(self.client.patch(url,json={'name':'Andet','new_name':'Other'},headers=self.headers).status_code,400)
        self.assertEqual(self.client.patch(url,json={'name':'New name','new_name':'Fritid'},headers=self.headers).status_code,400)

    def test_optional_budget_mapping_groups_categories_without_changing_spending(self):
        self.client.post('/api/categories',json={'name':'Custom'},headers=self.headers)
        with connect(self.db) as db:
            db.execute("INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category) VALUES ('demo-daily','mapping',?,'Mapping',-34500,'DKK','Custom')",(self.month+'-01',))
        before=self.client.get('/api/dashboard?source=demo').json
        endpoint='/api/categories'
        self.assertEqual(self.client.patch(endpoint,json={'name':'Custom','budget_category':'Andet'}).status_code,403)
        self.assertEqual(self.client.patch(endpoint,json={'name':'Custom','budget_category':'Unknown'},headers=self.headers).status_code,400)
        self.assertEqual(self.client.patch(endpoint,json={'name':'Custom','budget_category':'Andet'},headers=self.headers).status_code,200)
        after=self.client.get('/api/dashboard?source=demo').json
        self.assertEqual(after['expenses'],before['expenses'])
        self.assertEqual(after['budget_spent'],before['budget_spent']+34500)
        self.assertEqual(after['budget'],before['budget'])
        create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider})
        self.assertEqual(next(c for c in self.client.get(endpoint).json['items'] if c['name']=='Custom')['budget_category'],'Andet')
        self.client.patch(endpoint,json={'name':'Custom','budget_category':None},headers=self.headers)
        self.assertEqual(self.client.get('/api/dashboard?source=demo').json['budget_spent'],before['budget_spent'])

    def test_migration_preserves_existing_custom_budget_and_mapping(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'budget.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript("""CREATE TABLE categories(name TEXT PRIMARY KEY,color TEXT NOT NULL,protected INTEGER NOT NULL);
                    CREATE TABLE app_migrations(name TEXT PRIMARY KEY);
                    INSERT INTO app_migrations VALUES ('categories');
                    INSERT INTO categories VALUES ('Legacy','#809087',0);
                    CREATE TABLE budgets(source TEXT,month TEXT,currency TEXT,category TEXT,amount INTEGER,PRIMARY KEY(source,month,currency,category));
                    INSERT INTO budgets VALUES ('live','2026-01','DKK','Legacy',12345);""")
            db.close()
            migrated=create_app({'TESTING':True,'DATA_DIR':directory,'PROVIDER':self.provider})
            client=migrated.test_client()
            data=client.get('/api/categories').json
            self.assertEqual(data['items'][0]['budget_category'],'Legacy')
            self.assertTrue(data['items'][0]['requires_merchant'])
            with connect(path) as db:
                self.assertEqual(db.execute("SELECT amount FROM budgets WHERE category='Legacy'").fetchone()[0],12345)
            create_app({'TESTING':True,'DATA_DIR':directory,'PROVIDER':self.provider})
            self.assertEqual(client.get('/api/categories').json,data)

    def test_provider_pagination_continues_through_empty_page(self):
        provider=EnableBanking('unused','unused')
        with patch.object(provider,'request',side_effect=[{'transactions':[],'continuation_key':'next'}, {'transactions':[{'entry_reference':'a'}]}]) as req:
            self.assertEqual(provider.transactions('id','2026-01-01','2026-01-31'),[{'entry_reference':'a'}])
            first,second=[call.kwargs['params'] for call in req.call_args_list]
            self.assertEqual(first,{'transaction_status':'BOOK','date_from':'2026-01-01','date_to':'2026-01-31'})
            self.assertEqual(second,{**first,'continuation_key':'next'})
        with patch.object(provider,'request',return_value={'transactions':[]}) as req:
            self.assertEqual(provider.transactions('id',strategy='longest'),[])
            self.assertEqual(req.call_args.kwargs['params'],{'transaction_status':'BOOK','strategy':'longest'})
        with patch.object(provider,'request',return_value={'transactions':[],'continuation_key':'again'}),self.assertRaises(BankError):
            provider.transactions('id','2026-01-01','2026-01-31')

if __name__=='__main__':
    unittest.main()
