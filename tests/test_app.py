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

    def banks(self):
        return [{'name':'Test Bank','country':'DK','psu_types':['personal']}]

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

    def test_demo_and_real_are_separate_and_transfers_do_not_inflate_totals(self):
        demo=self.client.get('/api/dashboard?source=demo').json
        live=self.client.get('/api/dashboard?source=live').json
        self.assertEqual(len(demo['accounts']),3)
        self.assertEqual(demo['income'],3250000)
        self.assertEqual(live['income'],0)
        self.assertEqual(live['accounts'],[])
        euro=self.client.get('/api/dashboard?source=demo&currency=EUR').json
        self.assertEqual((euro['balance'],euro['income'],euro['expenses']),(0,0,0))

    def test_amounts_are_exact_and_invalid_precision_rejected(self):
        self.assertEqual(cents('0.29'),29)
        self.assertEqual(cents('-573.20'),-57320)
        for invalid in ['0.001','NaN','Infinity','hello']:
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                cents(invalid)

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

    def test_sync_refreshes_existing_account_name(self):
        self.client.post('/api/bank/connect',json={'bank':'Test Bank'},headers=self.headers)
        state=self.provider.calls[-1][2]['json']['state']
        self.client.get('/bank/callback?state='+state+'&code=abc')
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

    def test_category_management_moves_rules_transactions_and_budgets(self):
        url='/api/categories'
        self.assertEqual(self.client.post(url,json={'name':'Travel'}).status_code,403)
        self.assertEqual(self.client.post(url,json={'name':'Travel'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.post(url,json={'name':' TRAVEL '},headers=self.headers).status_code,400)
        self.assertIn('Travel',self.client.get('/api/config').json['categories'])
        self.assertIn('Travel',[c['name'] for c in self.client.get('/api/dashboard').json['categories']])
        with connect(self.db) as db:
            tid=db.execute("SELECT id FROM transactions WHERE account_id='demo-daily' LIMIT 1").fetchone()[0]
            db.execute("INSERT INTO budgets VALUES ('live','2025-01','DKK','Travel',12300)")
            db.execute("INSERT INTO budgets VALUES ('live','2025-01','DKK','Andet',1000)")
        self.assertEqual(self.client.patch('/api/transactions/'+str(tid),json={'category':'Travel'},headers=self.headers).status_code,200)
        self.assertEqual(self.client.delete(url,json={'name':'Travel','replacement':'Andet'},headers=self.headers).status_code,200)
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE id=?',(tid,)).fetchone()[0],'Andet')
            self.assertEqual(db.execute("SELECT count(*) FROM category_rules WHERE category='Travel'").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT amount FROM budgets WHERE source='live' AND month='2025-01' AND category='Andet'").fetchone()[0],13300)
        self.assertEqual(self.client.delete(url,json={'name':'Andet','replacement':'Bolig'},headers=self.headers).status_code,400)
        self.client.delete(url,json={'name':'Shopping','replacement':'Andet'},headers=self.headers)
        create_app({'TESTING':True,'DATA_DIR':self.temp.name,'PROVIDER':self.provider})
        self.assertNotIn('Shopping',self.client.get('/api/config').json['categories'])
        raw={'status':'BOOK','booking_date':'2026-09-01','credit_debit_indicator':'DBIT','transaction_amount':{'amount':'12','currency':'DKK'},'remittance_information':['Matas'],'entry_reference':'deleted-default'}
        import_account(self.db,'demo-daily',[raw],[])
        with connect(self.db) as db:
            self.assertEqual(db.execute('SELECT category FROM transactions WHERE external_id=?',(normalize_transactions([raw])[0][0],)).fetchone()[0],'Andet')

    def test_provider_pagination_continues_through_empty_page(self):
        provider=EnableBanking('unused','unused')
        with patch.object(provider,'request',side_effect=[{'transactions':[],'continuation_key':'next'}, {'transactions':[{'entry_reference':'a'}]}]) as req:
            self.assertEqual(provider.transactions('id','2026-01-01','2026-01-31'),[{'entry_reference':'a'}])
            first,second=[call.kwargs['params'] for call in req.call_args_list]
            self.assertEqual(second,{**first,'continuation_key':'next'})
        with patch.object(provider,'request',return_value={'transactions':[],'continuation_key':'again'}),self.assertRaises(BankError):
            provider.transactions('id','2026-01-01','2026-01-31')

if __name__=='__main__':
    unittest.main()
