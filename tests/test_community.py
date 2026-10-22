import re
import secrets
import sys
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from werkzeug.security import generate_password_hash
from app import create_app
from community import CommunityError
from community_protocol import CONSENT_VERSION, safe_pair,merchant_key
from community_crypto import make_request,open_response,open_request,make_response
from db import connect, initialize
from fjordbudget_server.app import create_app as create_server, connect as server_db
from merchant_matching import apply_learned_merchants

PASSWORD='test-only-password'
PASSWORD_HASH=generate_password_hash(PASSWORD,method='pbkdf2:sha256:1000')
TEXT='MCD 05755 STJERNE KEBAB'
NORMAL=TEXT.casefold()


class WireClient:
    """Real crypto boundary in tests; never bypass server authentication."""
    def __init__(self,server):
        self.client=server.test_client()
        self.public_key=server.extensions['vault'].public_key

    def open(self,path,method='GET',json=None,headers=None):
        if not path.startswith('/v1/'):
            return self.client.open(path,method=method,json=json,headers=headers)
        token=(headers or {}).get('Authorization','').removeprefix('Bearer ')
        envelope,context=make_request(self.public_key,method,path,token,json)
        response=self.client.post('/v1/exchange',json=envelope)
        data=open_response(context,response.json)
        return SimpleNamespace(status_code=response.status_code,json=data)

    def get(self,path,**kwargs):
        return self.open(path,'GET',**kwargs)

    def post(self,path,**kwargs):
        return self.open(path,'POST',**kwargs)


class CommunityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.server=create_server({'TESTING':True,'DATA_DIR':self.temp.name+'/server',
            'ADMIN_PASSWORD_HASH':PASSWORD_HASH,'SERVER_URL':'http://localhost:8088'})
        self.remote=WireClient(self.server)
        self.admin=self.server.test_client()
        self.admin.get('/login')
        with self.admin.session_transaction() as session:
            self.csrf=session['csrf']
        self.assertEqual(self.admin.post('/login',data={'csrf':self.csrf,'password':PASSWORD}).status_code,302)
        with self.admin.session_transaction() as session:
            self.csrf=session['csrf']
        self.client_number=0
        self.calls=[]

    def participant(self):
        self.client_number+=1
        app=create_app({'TESTING':True,'AUTH_MODE':'local','DATA_DIR':self.temp.name+'/app'+str(self.client_number),
            'COMMUNITY_SERVER_URL':'http://localhost:8088',
            'COMMUNITY_SERVER_PUBLIC_KEY':self.server.extensions['vault'].public_key})
        sync=app.extensions['community_sync']
        def transport(method,endpoint,token,payload=None):
            self.calls.append((method,endpoint,payload))
            response=self.remote.open(endpoint,method=method,json=payload,headers={'Authorization':'Bearer '+token})
            if response.status_code not in (200,201):
                if endpoint=='/v1/withdraw' and response.status_code in (401,403):
                    return {'ok':True}
                raise CommunityError('Test HTTP '+str(response.status_code))
            return response.json
        sync._call=transport
        with connect(app.extensions['db_path']) as db:
            db.execute("INSERT INTO accounts(id,source,name,bank,last4,currency) VALUES ('real','live','Private','Test','1234','DKK')")
        return app,sync

    def seed(self,app,merchant='Kebab alias',remember=True,manual=True,text=TEXT):
        with connect(app.extensions['db_path']) as db:
            result=db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,description,
                amount,currency,category,merchant,merchant_manual) VALUES ('real',?,'2026-10-01',?,-12345,'DKK','Fritid',?,?)''',
                (str(uuid.uuid4()),text,merchant,int(manual)))
            if remember:
                db.execute("INSERT OR REPLACE INTO merchant_rules VALUES ('live',?,?)",(text.casefold(),merchant))
        return result.lastrowid

    def proposal(self,merchant=None):
        with server_db(self.server.extensions['db_path']) as db:
            if merchant:
                row=db.execute('SELECT * FROM proposals WHERE merchant_key=?',(self.server.extensions['vault'].index(merchant_key(merchant)),)).fetchone()
            else:
                row=db.execute('SELECT * FROM proposals ORDER BY id DESC LIMIT 1').fetchone()
            item=dict(row)
            item['merchant']=self.server.extensions['vault'].decrypt(item['merchant'])
            return item

    def approve(self,name='Stjerne Kebab',proposal=None,replace=False):
        proposal=proposal or self.proposal()
        data={'csrf':self.csrf,'action':'approve','merchant':name}
        if replace:
            data['replace']='yes'
        return self.admin.post('/proposals/'+str(proposal['id'])+'/review',data=data)

    def row(self,app,transaction_id):
        with connect(app.extensions['db_path']) as db:
            return dict(db.execute('SELECT * FROM transactions WHERE id=?',(transaction_id,)).fetchone())

    def test_opt_in_is_required_and_credentials_are_not_exposed(self):
        app,sync=self.participant()
        self.seed(app)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.calls,[])
        client=app.test_client()
        html=client.get('/').text
        csrf=re.search('name="csrf-token" content="([^"]+)"',html)[1]
        status=client.get('/api/community').json
        self.assertFalse(status['enabled'])
        self.assertEqual(status['eligible'],1)
        self.assertNotIn('credential',status)
        self.assertNotIn('client_id',status)
        self.assertEqual(client.put('/api/community',json={'enabled':True,'consent_version':CONSENT_VERSION}).status_code,403)
        self.assertEqual(client.put('/api/community',headers={'X-CSRF-Token':csrf},json={'enabled':True}).status_code,400)
        self.assertEqual(client.put('/api/community',headers={'X-CSRF-Token':csrf},json={'enabled':True,'consent_version':CONSENT_VERSION}).status_code,200)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.proposal()['status'],'pending')
        for _,endpoint,payload in self.calls:
            if endpoint=='/v1/contributions':
                self.assertEqual(payload,{'items':[{'bank_text':NORMAL,'merchant':'Kebab alias'}]})
        initialize(app.extensions['db_path'])
        self.assertTrue(sync.status()['enabled'])
        self.assertEqual(self.remote.get('/v1/rules').status_code,401)
        token=secrets.token_urlsafe(48)
        self.assertEqual(self.remote.get('/v1/rules',headers={'Authorization':'Bearer '+token}).status_code,403)

    def test_admin_rename_roundtrip_updates_origin_and_other_participants(self):
        app,sync=self.participant()
        original=self.seed(app)
        exception=self.seed(app,merchant='Personlig undtagelse',remember=False)
        sync.configure(True,CONSENT_VERSION)
        self.assertTrue(sync.sync_once())
        self.assertEqual(sync.status()['cached'],0)
        self.assertEqual(self.approve().status_code,302)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.row(app,original)['merchant'],'Stjerne Kebab')
        self.assertEqual(self.row(app,original)['merchant_manual'],1)
        self.assertEqual(self.row(app,exception)['merchant'],'Personlig undtagelse')
        with connect(app.extensions['db_path']) as db:
            self.assertEqual(db.execute("SELECT merchant FROM merchant_rules WHERE source='live'").fetchone()[0],'Stjerne Kebab')
        # Canonical name updates are not re-uploaded as fresh contributions.
        sent=sum(endpoint=='/v1/contributions' for _,endpoint,_ in self.calls)
        self.assertTrue(sync.sync_once())
        self.assertEqual(sum(endpoint=='/v1/contributions' for _,endpoint,_ in self.calls),sent)
        other,other_sync=self.participant()
        unknown=self.seed(other,merchant=None,remember=False,manual=False)
        self.assertTrue(other_sync.sync_once())
        self.assertIsNone(self.row(other,unknown)['merchant'])
        other_sync.configure(True,CONSENT_VERSION)
        self.assertTrue(other_sync.sync_once())
        self.assertEqual(self.row(other,unknown)['merchant'],'Stjerne Kebab')
        self.assertEqual(self.row(other,unknown)['merchant_community'],1)
        self.assertEqual(other_sync.status()['shared'],0)
        # An administrator may subsequently rename an approved association too.
        self.assertEqual(self.approve('Stjerne Kebab & Grill',replace=True).status_code,302)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.row(app,original)['merchant'],'Stjerne Kebab & Grill')
        self.assertTrue(other_sync.sync_once())
        self.assertEqual(self.row(other,unknown)['merchant'],'Stjerne Kebab & Grill')
        # Revocation clears inferred common labels but retains private rules.
        self.assertEqual(self.admin.post('/proposals/'+str(self.proposal()['id'])+'/revoke',data={'csrf':self.csrf}).status_code,302)
        self.assertTrue(other_sync.sync_once())
        self.assertIsNone(self.row(other,unknown)['merchant'])
        self.assertEqual(self.row(other,unknown)['merchant_community'],0)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.row(app,original)['merchant'],'Stjerne Kebab & Grill')

    def test_newer_local_choice_survives_old_approval_and_can_be_reviewed(self):
        app,sync=self.participant()
        transaction=self.seed(app)
        sync.configure(True,CONSENT_VERSION)
        sync.sync_once()
        self.approve()
        with connect(app.extensions['db_path']) as db:
            db.execute("UPDATE merchant_rules SET merchant='Min nyere rettelse' WHERE source='live'")
            db.execute("UPDATE transactions SET merchant='Min nyere rettelse' WHERE id=?",(transaction,))
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.row(app,transaction)['merchant'],'Min nyere rettelse')
        pending=self.proposal('Min nyere rettelse')
        self.assertEqual(pending['status'],'pending')
        self.assertEqual(self.approve('Kebab endeligt',pending).status_code,409)
        self.assertEqual(self.approve('Kebab endeligt',pending,replace=True).status_code,302)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.row(app,transaction)['merchant'],'Kebab endeligt')

    def test_duplicates_merge_once_conflicts_wait_and_private_data_is_rejected(self):
        app,sync=self.participant()
        self.seed(app,merchant='Stjerne Kebab')
        sync.configure(True,CONSENT_VERSION);sync.sync_once();self.approve();sync.sync_once()
        with connect(app.extensions['db_path']) as db:
            credential=db.execute('SELECT credential FROM community_settings').fetchone()[0]
        token=app.extensions['cipher'].decrypt(credential.encode()).decode()
        headers={'Authorization':'Bearer '+token}
        pair={'bank_text':TEXT,'merchant':'Stjerne-Kebab'}
        for _ in range(2):
            response=self.remote.post('/v1/contributions',headers=headers,json={'items':[pair]})
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.json['items'][0]['status'],'merged')
        with server_db(self.server.extensions['db_path']) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM contributions').fetchone()[0],1)
        response=self.remote.post('/v1/contributions',headers=headers,json={'items':[{'bank_text':TEXT,'merchant':'Anden butik'}]})
        self.assertEqual(response.json['items'][0]['status'],'pending')
        self.assertEqual(self.remote.get('/v1/rules',headers=headers).json['items'][0]['merchant'],'Stjerne Kebab')
        for bad in ({**pair,'amount':1234},{'bank_text':'MobilePay Peter','merchant':'Peter'},
                    {'bank_text':'POS 1234567890 Butik','merchant':'Butik'},
                    {'bank_text':'POS mail@example.dk','merchant':'Butik'},
                    {'bank_text':TEXT,'merchant':'Name\nInjected'}):
            self.assertEqual(self.remote.post('/v1/contributions',headers=headers,json={'items':[bad]}).status_code,400)
        self.assertEqual(self.admin.post('/proposals/1/review',data={'action':'approve','merchant':'Other'}).status_code,403)
        self.assertEqual(self.remote.get('/').status_code,302)
        response=self.remote.get('/static/server.css')
        self.assertEqual(response.status_code,200)
        response.close()

    def test_opt_out_offline_removes_cache_and_blocks_new_network_and_matching(self):
        app,sync=self.participant()
        sync.configure(True,CONSENT_VERSION);sync.sync_once()
        transaction=self.seed(app,merchant=None,remember=False,manual=False)
        with connect(app.extensions['db_path']) as db:
            db.execute('INSERT INTO community_rules VALUES (?,?,1)',(NORMAL,'Fælles navn'))
            apply_learned_merchants(db,'live')
        self.assertEqual(self.row(app,transaction)['merchant'],'Fælles navn')
        sync.configure(False,None)
        self.assertEqual(sync.status()['cached'],0)
        self.assertIsNone(self.row(app,transaction)['merchant'])
        with patch.object(sync,'_call',side_effect=CommunityError('Offline')):
            self.assertFalse(sync.sync_once())
        self.assertTrue(sync.status()['revoke_pending'])
        self.assertTrue(sync.sync_once())
        self.assertFalse(sync.status()['revoke_pending'])
        before=len(self.calls)
        self.assertTrue(sync.sync_once())
        self.assertEqual(len(self.calls),before)
        with connect(app.extensions['db_path']) as db:
            secret=app.extensions['cipher'].decrypt(db.execute('SELECT credential FROM community_settings').fetchone()[0].encode()).decode()
        self.assertEqual(self.remote.get('/v1/rules',headers={'Authorization':'Bearer '+secret}).status_code,403)
        sync.configure(True,CONSENT_VERSION)
        self.assertTrue(sync.sync_once())

    def test_filter_never_contributes_demo_inferred_transfers_or_unremembered_choices(self):
        app,sync=self.participant()
        self.seed(app,remember=False,text='POS UNREMEMBERED SHOP')
        self.seed(app,manual=False,text='POS INFERRED SHOP')
        self.seed(app,text='MobilePay Person')
        self.seed(app,text='POS 1234567890 Secret')
        self.seed(app,text='POS SALARY SHOP')
        with connect(app.extensions['db_path']) as db:
            db.execute("UPDATE transactions SET category='Indkomst' WHERE description='POS SALARY SHOP'")
            db.execute("INSERT INTO merchant_rules VALUES ('demo','pos fictional','Demo')")
        sync.configure(True,CONSENT_VERSION)
        self.assertTrue(sync.sync_once())
        self.assertEqual(sync.status()['eligible'],0)
        self.assertEqual(sync.status()['shared'],0)
        self.assertGreater(sync.status()['excluded'],0)

    def test_configuration_and_wire_limits(self):
        with self.assertRaises(ValueError):
            create_server({'DATA_DIR':self.temp.name+'/no-password','ADMIN_PASSWORD_HASH':''})
        with self.assertRaises(ValueError):
            create_app({'TESTING':True,'AUTH_MODE':'local','DATA_DIR':self.temp.name+'/bad-url','COMMUNITY_SERVER_URL':'https://user:secret@example.dk'})
        app,sync=self.participant()
        sync.configure(True,CONSENT_VERSION);sync.sync_once()
        with self.assertRaises(ValueError):
            safe_pair('POS 010190-1234 Private','Shop')
        self.assertEqual(self.admin.post('/proposals/999/review',data={'csrf':self.csrf,'action':'approve','merchant':'Shop'}).status_code,404)
        self.assertEqual(self.server.test_client().get('/api/health',headers={'Host':'evil.example'}).status_code,400)

    def test_transport_tampering_replay_wrong_key_and_at_rest_ciphertext(self):
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from community_crypto import public_key
        app,sync=self.participant()
        self.seed(app)
        sync.configure(True,CONSENT_VERSION);self.assertTrue(sync.sync_once());self.approve()
        vault=self.server.extensions['vault']
        with server_db(self.server.extensions['db_path']) as db:
            for table in ('proposals','mappings'):
                row=db.execute('SELECT bank_text,merchant,merchant_key,bank_key FROM '+table).fetchone()
                self.assertNotIn('stjerne',str(tuple(row)).lower())
                self.assertNotIn('kebab',str(tuple(row)).lower())
                self.assertEqual(vault.decrypt(row['bank_text']),NORMAL)
        token=secrets.token_urlsafe(48)
        envelope,context=make_request(vault.public_key,'POST','/v1/enroll',token,
            {'client_id':str(uuid.uuid4()),'consent_version':CONSENT_VERSION})
        wire=self.server.test_client()
        first=wire.post('/v1/exchange',json=envelope)
        self.assertEqual(first.status_code,200)
        self.assertTrue(open_response(context,first.json)['ok'])
        replay=wire.post('/v1/exchange',json=envelope)
        self.assertEqual(replay.status_code,409)
        self.assertIn('allerede',open_response(context,replay.json)['error'])
        damaged=dict(envelope)
        damaged['box']=damaged['box'][:-4]+'AAAA'
        self.assertEqual(wire.post('/v1/exchange',json=damaged).status_code,400)
        wrong,_=make_request(public_key(X25519PrivateKey.generate()),'GET','/v1/rules',token,None)
        self.assertEqual(wire.post('/v1/exchange',json=wrong).status_code,400)
        self.assertEqual(wire.get('/v1/rules',headers={'Authorization':'Bearer '+token}).status_code,403)
        with self.assertRaises(ValueError):
            open_response(context,{'nonce':'AA==','box':'AAAA'})

    def test_opt_out_during_feed_does_not_repopulate_cache(self):
        app,sync=self.participant()
        sync.configure(True,CONSENT_VERSION)
        real_call=sync._call
        def withdraw_during_response(method,endpoint,token,payload=None):
            if endpoint.startswith('/v1/rules'):
                sync.configure(False,None)
                return {'items':[{'bank_key':NORMAL,'merchant':'Forhandler','active':1,'revision':1}],
                    'cursor':1,'has_more':False}
            return real_call(method,endpoint,token,payload)
        sync._call=withdraw_during_response
        self.assertFalse(sync.sync_once())
        self.assertFalse(sync.status()['enabled'])
        self.assertEqual(sync.status()['cached'],0)

    def test_restart_retains_server_keys_and_missing_keys_fail_closed(self):
        app,sync=self.participant()
        self.seed(app)
        sync.configure(True,CONSENT_VERSION);self.assertTrue(sync.sync_once());self.approve()
        config={'TESTING':True,'DATA_DIR':self.temp.name+'/server',
            'ADMIN_PASSWORD_HASH':PASSWORD_HASH,'SERVER_URL':'http://localhost:8088'}
        reopened=create_server(config)
        self.assertEqual(reopened.extensions['vault'].public_key,self.server.extensions['vault'].public_key)
        with server_db(reopened.extensions['db_path']) as db:
            text=db.execute('SELECT bank_text FROM mappings').fetchone()[0]
            self.assertEqual(reopened.extensions['vault'].decrypt(text),NORMAL)
        # This is an isolated temporary fixture, not a real server's key.
        (Path(config['DATA_DIR'])/'keys'/'storage.key').unlink()
        with self.assertRaises(ValueError):
            create_server(config)

    def test_complete_initial_library_is_uploaded_before_any_shared_download(self):
        app,sync=self.participant()
        ids=[]
        for index in range(30):
            ids.append(self.seed(app,merchant=f'Butik {index:02d}',text=f'POS SHOP {index:02d}'))
        sync.configure(True,CONSENT_VERSION)
        self.assertTrue(sync.sync_once())
        self.assertEqual(sync.status()['upload_remaining'],5)
        self.assertEqual(sync.status()['shared'],25)
        self.assertFalse(any(endpoint.startswith('/v1/rules') or endpoint=='/v1/resolutions' for _,endpoint,_ in self.calls))
        for index,transaction in enumerate(ids):
            self.assertEqual(self.row(app,transaction)['merchant'],f'Butik {index:02d}')
        self.assertTrue(sync.sync_once())
        self.assertEqual(sync.status()['upload_remaining'],0)
        self.assertEqual(sync.status()['awaiting_review'],30)
        self.assertTrue(any(endpoint.startswith('/v1/rules') for _,endpoint,_ in self.calls))
        self.assertEqual(self.approve('Godkendt butik',self.proposal('Butik 29')).status_code,302)
        self.assertTrue(sync.sync_once())
        self.assertEqual(self.row(app,ids[29])['merchant'],'Godkendt butik')
        self.assertEqual(self.row(app,ids[0])['merchant'],'Butik 00')

    def test_merge_into_existing_merchant_can_rename_the_combined_group(self):
        first,first_sync=self.participant()
        first_id=self.seed(first)
        first_sync.configure(True,CONSENT_VERSION);self.assertTrue(first_sync.sync_once())
        self.approve();self.assertTrue(first_sync.sync_once())
        second,second_sync=self.participant()
        second_id=self.seed(second,merchant='Andet alias',text='POS STJERNE RESTAURANT')
        second_sync.configure(True,CONSENT_VERSION);self.assertTrue(second_sync.sync_once())
        proposal=self.proposal('Andet alias')
        data={'csrf':self.csrf,'action':'approve','merge_into':'Stjerne Kebab','merchant':'Stjerne Kebab & Grill'}
        self.assertEqual(self.admin.post(f'/proposals/{proposal["id"]}/review',data=data).status_code,409)
        data['replace']='yes'
        self.assertEqual(self.admin.post(f'/proposals/{proposal["id"]}/review',data=data).status_code,302)
        self.assertTrue(first_sync.sync_once());self.assertTrue(second_sync.sync_once())
        self.assertEqual(self.row(first,first_id)['merchant'],'Stjerne Kebab & Grill')
        self.assertEqual(self.row(second,second_id)['merchant'],'Stjerne Kebab & Grill')


if __name__=='__main__':
    unittest.main()
