"""Opt-in community sync. No network calls occur during transaction matching."""
import json
import secrets
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from flask import jsonify, request

from community_protocol import CONSENT_VERSION, key, safe_pair
from community_crypto import make_request,open_response,decode
from db import connect, transaction_title


class CommunityError(Exception):
    pass


class SharingStopped(CommunityError):
    """A consent change cancelled this pass; it is not a server failure."""


def shareable_rules(db):
    items, excluded = [], 0
    confirmed={(row['title'],row['merchant']) for row in db.execute('''
        SELECT DISTINCT transaction_title(t.description) title,t.merchant
        FROM transactions t JOIN accounts a ON a.id=t.account_id
        WHERE a.source='live' AND t.amount<0 AND t.category NOT IN ('Indkomst','Overførsler')
        AND t.merchant_manual=1 AND t.merchant IS NOT NULL''')}
    for row in db.execute("SELECT title,merchant FROM merchant_rules WHERE source='live' ORDER BY title"):
        # The remembered purchase rule must be backed by an actual outgoing card
        # purchase, not a transfer, salary, demo transaction or inferred label.
        try:
            if (row['title'],row['merchant']) not in confirmed:
                raise ValueError('Ikke et bekræftet kortkøb.')
            text,name = safe_pair(row['title'],row['merchant'])
        except ValueError:
            excluded += 1
            continue
        items.append({'title':row['title'],'bank_text':text,'merchant':name})
    return items,excluded


def community_lookup(db, source):
    if source != 'live':
        return {}
    setting = db.execute('SELECT enabled FROM community_settings WHERE id=1').fetchone()
    if not setting or not setting['enabled']:
        return {}
    return {row['bank_key']:row['merchant'] for row in db.execute('SELECT bank_key,merchant FROM community_rules')}


class CommunitySync:
    def __init__(self, app, db_path, cipher):
        self.app,self.path,self.cipher = app,db_path,cipher
        self.lock = threading.RLock()
        self.run_lock = threading.Lock()
        self.wake = threading.Event()
        self.running = False
        self.enrolled = False
        self.url = app.config['COMMUNITY_SERVER_URL'].rstrip('/')
        self.public_key=app.config['COMMUNITY_SERVER_PUBLIC_KEY'].strip()
        if self.public_key:
            decode(self.public_key,32)
        if self.url:
            parsed=urlparse(self.url)
            if (parsed.scheme!='https' and not (app.testing and parsed.scheme=='http')) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ('','/'):
                raise ValueError('COMMUNITY_SERVER_URL skal være en HTTPS-adresse uden sti eller login.')
        with connect(self.path) as db:
            db.execute('''INSERT OR IGNORE INTO community_settings(id,client_id,credential)
                VALUES (1,?,?)''',(str(uuid.uuid4()),cipher.encrypt(secrets.token_urlsafe(48).encode()).decode()))

    def status(self):
        with connect(self.path) as db:
            row = dict(db.execute('SELECT enabled,revoke_pending,last_sync,error,consent_version FROM community_settings WHERE id=1').fetchone())
            items,excluded=shareable_rules(db)
            sent={entry['title']:dict(entry) for entry in db.execute('SELECT title,local_merchant,status FROM community_sent')}
            upload_remaining=sum(item['title'] not in sent or sent[item['title']]['local_merchant']!=item['merchant']
                or sent[item['title']]['status']=='unknown' for item in items)
            row.update(configured=bool(self.url and self.public_key),server=self.url,enabled=bool(row['enabled']),running=self.running,
                eligible=len(items),excluded=excluded,cached=db.execute('SELECT count(*) FROM community_rules').fetchone()[0],
                shared=db.execute('SELECT count(*) FROM community_sent').fetchone()[0],
                upload_remaining=upload_remaining,awaiting_review=sum(item['status']=='pending' for item in sent.values()),
                preview=[{'bank_text':item['bank_text'],'merchant':item['merchant']} for item in items[:100]],
                consent_version=CONSENT_VERSION)
        return row

    def configure(self, enabled, consent):
        from merchant_matching import apply_learned_merchants
        if type(enabled) is not bool:
            raise ValueError('Vælg om du vil dele banktekster.')
        if enabled and (not self.url or not self.public_key or consent != CONSENT_VERSION):
            raise ValueError('Serveren skal være opsat, og delingen skal godkendes.')
        # Serialize consent changes with outbound calls: none can begin after opt-out.
        with self.lock,connect(self.path) as db:
            self.enrolled=False
            db.execute('UPDATE community_settings SET enabled=?,revoke_pending=?,consent_version=?,error=NULL WHERE id=1',
                (int(enabled),int(not enabled),CONSENT_VERSION if enabled else None))
            if not enabled:
                db.execute('DELETE FROM community_rules')
                db.execute('UPDATE community_settings SET cursor=0 WHERE id=1')
                apply_learned_merchants(db,'live')
        self.wake.set()

    def call(self,method,endpoint,token,payload=None):
        with self.lock:
            with connect(self.path) as db:
                enabled=db.execute('SELECT enabled FROM community_settings WHERE id=1').fetchone()[0]
            if bool(enabled)==(endpoint=='/v1/withdraw'):
                raise SharingStopped('Delingsvalget er ændret.')
            return self._call(method,endpoint,token,payload)

    @contextmanager
    def active_db(self):
        with self.lock,connect(self.path) as db:
            if not db.execute('SELECT enabled FROM community_settings WHERE id=1').fetchone()[0]:
                raise SharingStopped('Delingen er slået fra.')
            yield db

    def _call(self,method,endpoint,token,payload=None):
        try:
            envelope,context=make_request(self.public_key,method,endpoint,token,payload)
            # Bounded responses, TLS verification, no redirects to a different host.
            with requests.request('POST',self.url+'/v1/exchange',json=envelope,
                    timeout=(4,12),allow_redirects=False,stream=True) as response:
                if endpoint=='/v1/withdraw' and response.status_code in (401,403):
                    return {'ok':True}
                if response.status_code not in (200,201):
                    if response.status_code in (401,403):
                        self.enrolled=False
                    raise CommunityError('Fællesserveren afviste forespørgslen (HTTP %s).'%response.status_code)
                data=bytearray()
                for chunk in response.iter_content(16384):
                    data.extend(chunk)
                    if len(data)>524288:
                        raise CommunityError('Fællesserverens svar var for stort.')
                return open_response(context,json.loads(data))
        except (requests.RequestException,ValueError) as error:
            raise CommunityError('Kunne ikke kontakte FjordBudget Server. Prøver igen automatisk.') from error

    def sync_once(self):
        from merchant_matching import apply_learned_merchants
        if not self.url or not self.run_lock.acquire(blocking=False):
            return False
        self.running=True
        try:
            with connect(self.path) as db:
                setting=dict(db.execute('SELECT * FROM community_settings WHERE id=1').fetchone())
            token=self.cipher.decrypt(setting['credential'].encode()).decode()
            if not setting['enabled']:
                if setting['revoke_pending']:
                    # Enrollment may never have succeeded. A missing remote identity
                    # is equivalent to withdrawn access; it must not block local opt-out.
                    try:
                        self.call('POST','/v1/withdraw',token,{})
                    except CommunityError:
                        raise
                    with connect(self.path) as db:
                        db.execute('UPDATE community_settings SET revoke_pending=0,error=NULL WHERE id=1')
                return True
            if not self.enrolled:
                self.call('POST','/v1/enroll',token,{'client_id':setting['client_id'],'consent_version':CONSENT_VERSION})
                self.enrolled=True
            with connect(self.path) as db:
                items,_=shareable_rules(db)
                sent={row['title']:dict(row) for row in db.execute('SELECT * FROM community_sent')}
            changed=[item for item in items if item['title'] not in sent or sent[item['title']]['local_merchant']!=item['merchant'] or sent[item['title']]['status']=='unknown']
            # One bounded batch per pass: large libraries drain over subsequent passes.
            if changed:
                batch=changed[:25]
                result=self.call('POST','/v1/contributions',token,{'items':[{'bank_text':item['bank_text'],'merchant':item['merchant']} for item in batch]})
                if not isinstance(result,dict) or not isinstance(result.get('items'),list) or len(result['items'])!=len(batch):
                    raise CommunityError('Ugyldigt svar fra fællesserveren.')
                with self.active_db() as db:
                    for item,decision in zip(batch,result['items']):
                        if decision.get('bank_text')!=item['bank_text'] or decision.get('status') not in ('pending','merged','rejected'):
                            raise CommunityError('Ugyldigt svar fra fællesserveren.')
                        db.execute('''INSERT INTO community_sent VALUES (?,?,?,?,?) ON CONFLICT(title) DO UPDATE SET
                            bank_text=excluded.bank_text,submitted_merchant=excluded.submitted_merchant,
                            local_merchant=excluded.local_merchant,status=excluded.status''',
                            (item['title'],item['bank_text'],item['merchant'],item['merchant'],decision['status']))
            # Existing remembered choices must visit the server first. Do not
            # import or adopt any replies until the complete upload queue drains.
            # Pending review never clears or replaces the app's current names.
            if len(changed)>25:
                self.wake.set()
                return True
            # Incremental global cache, including revoked-rule tombstones.
            after=setting['cursor']
            for _ in range(20):
                result=self.call('GET','/v1/rules?after='+str(after),token)
                if not isinstance(result,dict) or not isinstance(result.get('items'),list) or len(result['items'])>200 or type(result.get('cursor')) is not int or result['cursor']<after:
                    raise CommunityError('Ugyldig regeloversigt fra fællesserveren.')
                with self.active_db() as db:
                    for item in result['items']:
                        text,name=safe_pair(item.get('bank_key'),item.get('merchant'))
                        if type(item.get('revision')) is not int or item['revision']<=after or item['revision']>result['cursor'] or item.get('active') not in (0,1):
                            raise CommunityError('Ugyldig regel fra fællesserveren.')
                        if item['active']:
                            db.execute('INSERT INTO community_rules VALUES (?,?,?) ON CONFLICT(bank_key) DO UPDATE SET merchant=excluded.merchant,revision=excluded.revision',(text,name,item['revision']))
                        else:
                            db.execute('DELETE FROM community_rules WHERE bank_key=?',(text,))
                    db.execute('UPDATE community_settings SET cursor=? WHERE id=1',(result['cursor'],))
                if not result.get('has_more') or result['cursor']==after:
                    break
                after=result['cursor']
            # Ask for decisions on our own submissions. A local edit made since
            # submission is authoritative until that newer pair is approved.
            with connect(self.path) as db:
                submitted=[dict(row) for row in db.execute('SELECT * FROM community_sent WHERE title>? ORDER BY title LIMIT 500',(setting['resolution_cursor'],))]
                if not submitted:
                    submitted=[dict(row) for row in db.execute('SELECT * FROM community_sent ORDER BY title LIMIT 500')]
            for offset in range(0,len(submitted),25):
                batch=submitted[offset:offset+25]
                result=self.call('POST','/v1/resolutions',token,{'items':[{'bank_text':row['bank_text'],'merchant':row['submitted_merchant']} for row in batch]})
                if not isinstance(result,dict) or not isinstance(result.get('items'),list) or len(result['items'])!=len(batch):
                    raise CommunityError('Ugyldigt svar om godkendelse.')
                with self.active_db() as db:
                    for old,decision in zip(batch,result['items']):
                        if decision.get('bank_text')!=old['bank_text'] or decision.get('merchant')!=old['submitted_merchant']:
                            raise CommunityError('Svaret vedrører en anden banktekst.')
                        canonical=decision.get('canonical_merchant')
                        db.execute('UPDATE community_sent SET status=? WHERE title=?',(decision['status'],old['title']))
                        if canonical and decision['status']=='merged':
                            _,canonical=safe_pair(old['bank_text'],canonical)
                            current=db.execute("SELECT merchant FROM merchant_rules WHERE source='live' AND title=?",(old['title'],)).fetchone()
                            if current and current['merchant']==old['local_merchant']:
                                db.execute("UPDATE merchant_rules SET merchant=? WHERE source='live' AND title=?",(canonical,old['title']))
                                db.execute('''UPDATE transactions SET merchant=?,merchant_community=0
                                    WHERE account_id IN (SELECT id FROM accounts WHERE source='live')
                                    AND transaction_title(description)=? AND merchant=?''',(canonical,old['title'],old['local_merchant']))
                                db.execute('UPDATE community_sent SET local_merchant=? WHERE title=?',(canonical,old['title']))
                    db.execute('UPDATE community_settings SET resolution_cursor=? WHERE id=1',(batch[-1]['title'],))
            with self.active_db() as db:
                apply_learned_merchants(db,'live')
                db.execute('UPDATE community_settings SET last_sync=?,error=NULL WHERE id=1',
                    (datetime.now(timezone.utc).isoformat(timespec='seconds'),))
            return True
        except SharingStopped:
            return False
        except Exception as error:
            # Never log a bank text, credential or upstream response.
            message=str(error) if isinstance(error,CommunityError) else 'Synkroniseringen kunne ikke gennemføres. Prøver igen automatisk.'
            with connect(self.path) as db:
                db.execute('UPDATE community_settings SET error=? WHERE id=1',(message,))
            self.app.logger.warning('Community sync failed: %s',type(error).__name__)
            return False
        finally:
            self.running=False
            self.run_lock.release()

    def run(self):
        while True:
            self.sync_once()
            self.wake.wait(600)
            self.wake.clear()


def register_community(app,db_path,cipher):
    sync=CommunitySync(app,db_path,cipher)
    app.extensions['community_sync']=sync

    @app.get('/api/community')
    def community_status():
        return jsonify(sync.status())

    @app.get('/api/community/progress')
    def community_progress():
        with connect(db_path) as db:
            row=db.execute('SELECT enabled,last_sync FROM community_settings WHERE id=1').fetchone()
        return jsonify(enabled=bool(row['enabled']),last_sync=row['last_sync'])

    @app.put('/api/community')
    def community_configure():
        body=request.get_json(silent=True)
        if not isinstance(body,dict):
            raise ValueError('Ugyldigt valg.')
        sync.configure(body.get('enabled'),body.get('consent_version'))
        return jsonify(sync.status())

    @app.post('/api/community/sync')
    def community_sync():
        if not sync.status()['enabled']:
            return jsonify(error='Slå deling til først.'),403
        sync.wake.set()
        return jsonify(ok=True)

    if not app.testing and sync.url:
        threading.Thread(target=sync.run,daemon=True,name='fjordbudget-community').start()
