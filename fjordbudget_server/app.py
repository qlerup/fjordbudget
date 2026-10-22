import hashlib
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash

from community_protocol import CONSENT_VERSION, key, merchant_key, safe_pair
from community_crypto import open_request,make_response
from fjordbudget_server.keys import Vault


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


@contextmanager
def connect(path):
    db = sqlite3.connect(path, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        with db:
            yield db
    finally:
        db.close()


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATA_DIR=os.environ.get('DATA_DIR', '/data'),
        SERVER_URL=os.environ.get('SERVER_URL', 'http://localhost:8088').rstrip('/'),
        ADMIN_PASSWORD_HASH=os.environ.get('ADMIN_PASSWORD_HASH', ''),
        MAX_CONTENT_LENGTH=49152, SESSION_COOKIE_NAME='fjordbudget_server_session',
        SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Strict',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=8))
    if config:
        app.config.update(config)
    if not app.config['ADMIN_PASSWORD_HASH']:
        raise ValueError('ADMIN_PASSWORD_HASH skal angives. Se serverens installationsvejledning.')
    origin = app.config['SERVER_URL']
    parsed = urlparse(origin)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.path:
        raise ValueError('SERVER_URL skal være serverens fulde adresse uden sti.')
    app.config['TRUSTED_HOSTS'] = [parsed.hostname]
    app.config['SESSION_COOKIE_SECURE'] = parsed.scheme == 'https'
    directory = Path(app.config['DATA_DIR'])
    directory.mkdir(parents=True, exist_ok=True)
    secret_file = directory/'session.key'
    try:
        fd = os.open(secret_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as file:
            file.write(secrets.token_bytes(32))
    except FileExistsError:
        pass
    app.secret_key = secret_file.read_bytes()
    path = directory/'community.sqlite3'
    app.extensions['db_path'] = path
    vault=Vault(directory)
    app.extensions['vault']=vault
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
            CREATE TABLE IF NOT EXISTS clients (
                id TEXT PRIMARY KEY, token_hash TEXT NOT NULL UNIQUE,
                active INTEGER NOT NULL, consent_version TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS mappings (
                id INTEGER PRIMARY KEY, bank_key TEXT NOT NULL UNIQUE,
                bank_text TEXT NOT NULL,
                merchant TEXT NOT NULL, merchant_key TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS proposals (
                id INTEGER PRIMARY KEY, bank_text TEXT NOT NULL, merchant TEXT NOT NULL,
                bank_key TEXT NOT NULL,
                merchant_key TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                reviewed_at TEXT, reason TEXT,
                UNIQUE(bank_key,merchant_key));
            CREATE TABLE IF NOT EXISTS contributions (
                proposal_id INTEGER NOT NULL REFERENCES proposals(id),
                client_id TEXT NOT NULL REFERENCES clients(id),
                PRIMARY KEY(proposal_id,client_id));
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY,value INTEGER NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('revision',0);
            CREATE TABLE IF NOT EXISTS rate_limits (
                bucket TEXT PRIMARY KEY, window INTEGER NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, action TEXT NOT NULL, proposal_id INTEGER, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS replay_guard (id TEXT PRIMARY KEY,expires INTEGER NOT NULL);
        ''')

    def payload():
        return g.wire_body

    def readable(row):
        item=dict(row)
        for field in ('bank_text','merchant','current_merchant','canonical_merchant'):
            if item.get(field):
                item[field]=vault.decrypt(item[field])
        return item

    def limit(bucket, maximum, seconds=60):
        window = int(time.time()) // seconds * seconds
        # Persist across worker processes and restarts; do not store raw IPs.
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM rate_limits WHERE window<?', (int(time.time())-1200,))
            db.execute('''INSERT INTO rate_limits VALUES (?,?,1) ON CONFLICT(bucket)
                DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,
                window=excluded.window''', (bucket, window))
            count = db.execute('SELECT count FROM rate_limits WHERE bucket=?', (bucket,)).fetchone()[0]
        if count > maximum:
            abort(429, 'For mange forespørgsler. Prøv igen senere.')

    def token_hash():
        token=g.wire_token
        if not isinstance(token,str) or not 40 <= len(token) <= 128:
            abort(401, 'Bidragyderadgang mangler.')
        return hashlib.sha256(token.encode()).hexdigest()

    @app.before_request
    def protect():
        if request.routing_exception:
            raise request.routing_exception
        if request.path.startswith('/v1/'):
            if request.endpoint!='exchange':
                abort(403,'Kun krypterede app-til-server-beskeder accepteres.')
            address=hashlib.sha256((request.remote_addr or '').encode()).hexdigest()
            limit('exchange:'+address,300)
            body,context=open_request(vault.private,request.get_json(silent=True))
            g.wire_context=context
            target=body['path']
            if not isinstance(target,str) or len(target)>100:
                raise ValueError('Ugyldig krypteret destination.')
            parsed_target=urlparse(target)
            allowed={('POST','/v1/enroll'),('POST','/v1/withdraw'),('POST','/v1/contributions'),('POST','/v1/resolutions'),('GET','/v1/rules')}
            if not isinstance(body['method'],str) or (body['method'],parsed_target.path) not in allowed or parsed_target.scheme or parsed_target.netloc:
                raise ValueError('Ugyldig krypteret destination.')
            g.wire_path=parsed_target.path
            g.wire_args=parse_qs(parsed_target.query)
            g.wire_body=body['body']
            g.wire_token=body['token']
            # Replays are rejected across worker processes, including enrollment.
            replay_id=hashlib.sha256(context[1]).hexdigest()
            with connect(path) as db:
                db.execute('DELETE FROM replay_guard WHERE expires<?',(int(time.time()),))
                try:
                    db.execute('INSERT INTO replay_guard VALUES (?,?)',(replay_id,int(time.time())+600))
                except sqlite3.IntegrityError:
                    abort(409,'Beskeden er allerede modtaget.')
            digest = token_hash()
            limit('token:'+digest, 120)
            g.token_hash = digest
            if g.wire_path == '/v1/enroll':
                return
            with connect(path) as db:
                g.client = db.execute('SELECT * FROM clients WHERE token_hash=?', (digest,)).fetchone()
            if not g.client or (not g.client['active'] and g.wire_path != '/v1/withdraw'):
                abort(403, 'Deling skal være slået til for at hente fælles regler.')
            return
        if request.endpoint in ('health','static'):
            return
        session.setdefault('csrf', secrets.token_urlsafe(32))
        if request.method == 'POST':
            supplied = request.form.get('csrf', '')
            if not secrets.compare_digest(session['csrf'], supplied):
                abort(403, 'Sessionen er udløbet. Genindlæs siden.')
            if request.headers.get('Origin') not in (None, app.config['SERVER_URL']):
                abort(403, 'Ugyldig oprindelse.')
        if request.endpoint != 'login' and not session.get('admin'):
            return redirect('/login')

    @app.after_request
    def headers(response):
        if hasattr(g,'wire_context'):
            # Errors are authenticated and encrypted too, once a valid exchange
            # context exists. Cookies are never used by the contributor API.
            response.set_data(app.json.dumps(make_response(g.wire_context,response.get_json())))
            response.content_type='application/json'
        response.headers.update({'Cache-Control':'no-store', 'X-Content-Type-Options':'nosniff',
            'Referrer-Policy':'same-origin',
            'Content-Security-Policy':"default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"})
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.error('Server request failed: %s',type(error).__name__)
        return jsonify(error='Serveren kunne ikke gennemføre forespørgslen. Prøv igen.'),500

    @app.get('/api/health')
    def health():
        with connect(path) as db:
            db.execute('SELECT 1').fetchone()
        return jsonify(ok=True)

    @app.post('/v1/exchange')
    def exchange():
        return {'/v1/enroll':enroll,'/v1/withdraw':withdraw,
            '/v1/contributions':contribute,'/v1/resolutions':resolutions,'/v1/rules':rules}[g.wire_path]()

    @app.post('/v1/enroll')
    def enroll():
        body = payload()
        if not isinstance(body, dict) or set(body) != {'client_id', 'consent_version'}:
            raise ValueError('Ugyldig tilmelding.')
        if body['consent_version'] != CONSENT_VERSION:
            raise ValueError('Opdatér appen og godkend den aktuelle delingsbeskrivelse.')
        try:
            client_id = str(uuid.UUID(body['client_id']))
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Ugyldigt installations-id.') from None
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT token_hash FROM clients WHERE id=?', (client_id,)).fetchone()
            if existing and existing['token_hash'] != g.token_hash:
                abort(403, 'Installationen har en anden adgangsnøgle.')
            if not existing:
                # The outer exchange/IP limiter bounds enrollment as well.
                total=db.execute('SELECT count(*) FROM clients').fetchone()[0]
                if total>=100000:
                    abort(503,'Serveren kan ikke modtage flere installationer lige nu.')
            db.execute('''INSERT INTO clients VALUES (?,?,1,?,?) ON CONFLICT(id)
                DO UPDATE SET active=1,consent_version=excluded.consent_version,updated_at=excluded.updated_at''',
                (client_id,g.token_hash,CONSENT_VERSION,timestamp()))
        return jsonify(ok=True)

    @app.post('/v1/withdraw')
    def withdraw():
        with connect(path) as db:
            db.execute('UPDATE clients SET active=0,updated_at=? WHERE id=?', (timestamp(),g.client['id']))
        return jsonify(ok=True)

    @app.post('/v1/contributions')
    def contribute():
        body = payload()
        if not isinstance(body, dict) or set(body) != {'items'} or not isinstance(body['items'], list) or not 1 <= len(body['items']) <= 25:
            raise ValueError('Send mellem 1 og 25 banktekster ad gangen.')
        pairs = []
        for item in body['items']:
            if not isinstance(item, dict) or set(item) != {'bank_text','merchant'}:
                raise ValueError('Kun banktekst og forhandlernavn må sendes.')
            pairs.append(safe_pair(item['bank_text'],item['merchant']))
        results = []
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            for text, merchant in pairs:
                owner = vault.index(merchant_key(merchant))
                text_key=vault.index(text)
                known = db.execute('SELECT * FROM mappings WHERE bank_key=? AND active=1',(text_key,)).fetchone()
                state = 'merged' if known and known['merchant_key'] == owner else 'pending'
                reason = 'Kendt banktekst med et andet forhandlernavn' if known and state == 'pending' else 'Ny sammenhæng'
                db.execute('''INSERT INTO proposals(bank_text,merchant,bank_key,merchant_key,status,first_seen,last_seen,reason)
                    VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(bank_key,merchant_key) DO UPDATE SET last_seen=excluded.last_seen,
                    status=CASE WHEN excluded.status='merged' THEN 'merged' ELSE proposals.status END''',
                    (vault.encrypt(text),known['merchant'] if state=='merged' else vault.encrypt(merchant),text_key,owner,state,timestamp(),timestamp(),reason))
                proposal = db.execute('SELECT id,status FROM proposals WHERE bank_key=? AND merchant_key=?',(text_key,owner)).fetchone()
                db.execute('INSERT OR IGNORE INTO contributions VALUES (?,?)',(proposal['id'],g.client['id']))
                results.append({'bank_text':text,'status':proposal['status']})
        return jsonify(items=results)

    @app.get('/v1/rules')
    def rules():
        try:
            after = int(g.wire_args.get('after',['0'])[0])
        except ValueError:
            raise ValueError('Ugyldig markør.') from None
        if after < 0:
            raise ValueError('Ugyldig markør.')
        with connect(path) as db:
            rows = db.execute('SELECT bank_text,merchant,active,revision FROM mappings WHERE revision>? ORDER BY revision LIMIT 200',(after,)).fetchall()
        items=[]
        for row in rows:
            item=readable(row)
            item['bank_key']=item.pop('bank_text')
            items.append(item)
        return jsonify(items=items, cursor=rows[-1]['revision'] if rows else after, has_more=len(rows)==200)

    @app.post('/v1/resolutions')
    def resolutions():
        body=payload()
        if not isinstance(body,dict) or set(body)!={'items'} or not isinstance(body['items'],list) or len(body['items'])>25:
            raise ValueError('Ugyldig forespørgsel.')
        items=[]
        with connect(path) as db:
            for pair in body['items']:
                if not isinstance(pair,dict) or set(pair)!={'bank_text','merchant'}:
                    raise ValueError('Kun banktekst og forhandlernavn må sendes.')
                text,name=safe_pair(pair['bank_text'],pair['merchant'])
                row=db.execute('''SELECT p.status,m.merchant canonical_merchant FROM proposals p
                    JOIN contributions c ON c.proposal_id=p.id
                    LEFT JOIN mappings m ON m.bank_key=p.bank_key AND m.active=1
                    WHERE p.bank_key=? AND p.merchant_key=? AND c.client_id=?''',
                    (vault.index(text),vault.index(merchant_key(name)),g.client['id'])).fetchone()
                items.append({'bank_text':text,'merchant':name,'status':row['status'] if row else 'unknown',
                    'canonical_merchant':vault.decrypt(row['canonical_merchant']) if row and row['canonical_merchant'] and row['status']=='merged' else None})
        return jsonify(items=items)

    @app.route('/login', methods=['GET','POST'])
    def login():
        error = None
        if request.method == 'POST':
            address = hashlib.sha256((request.remote_addr or '').encode()).hexdigest()
            limit('login:'+address, 10, seconds=600)
            if check_password_hash(app.config['ADMIN_PASSWORD_HASH'],request.form.get('password','')):
                session.clear()
                session.update(admin=True,csrf=secrets.token_urlsafe(32))
                session.permanent=True
                return redirect('/')
            error = 'Forkert adgangskode.'
        return render_template('login.html',error=error)

    @app.post('/logout')
    def logout():
        session.clear()
        return redirect('/login')

    @app.get('/')
    def dashboard():
        state = request.args.get('status','pending')
        if state not in ('pending','merged','rejected'):
            raise ValueError('Ugyldigt filter.')
        search = request.args.get('q','')[:200]
        try:
            page = max(1,min(100000,int(request.args.get('page','1'))))
        except ValueError:
            raise ValueError('Ugyldigt sidetal.') from None
        with connect(path) as db:
            counts = {row['status']:row['n'] for row in db.execute('SELECT status,count(*) n FROM proposals GROUP BY status')}
            raw_rows = db.execute('''SELECT p.*,m.merchant current_merchant,
                (SELECT count(*) FROM contributions WHERE proposal_id=p.id) contributors
                FROM proposals p LEFT JOIN mappings m ON m.bank_key=p.bank_key AND m.active=1
                WHERE p.status=? ORDER BY p.last_seen DESC,p.id DESC''',(state,))
            rows=[]
            skipped=0
            for row in raw_rows:
                item=readable(row)
                if search and key(search) not in key(item['bank_text']) and key(search) not in key(item['merchant']):
                    continue
                if skipped<(page-1)*25:
                    skipped+=1
                    continue
                rows.append(item)
                if len(rows)==26:
                    break
            total = db.execute('SELECT count(*) FROM mappings WHERE active=1').fetchone()[0]
            clients = db.execute('SELECT count(*) FROM clients WHERE active=1').fetchone()[0]
            names = sorted({vault.decrypt(row[0]) for row in db.execute('SELECT merchant FROM mappings WHERE active=1 LIMIT 1000')})
        return render_template('dashboard.html',items=rows[:25],has_more=len(rows)>25,
            counts=counts,total=total,clients=clients,status=state,q=search,page=page,names=names)

    @app.post('/proposals/<int:proposal_id>/review')
    def review(proposal_id):
        action = request.form.get('action')
        if action not in ('approve','reject'):
            raise ValueError('Ugyldig handling.')
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            proposal = db.execute('SELECT * FROM proposals WHERE id=?',(proposal_id,)).fetchone()
            if not proposal:
                abort(404)
            if proposal['status'] not in ('pending','merged'):
                abort(409,'Forslaget er allerede behandlet. Genindlæs siden.')
            if action == 'approve':
                text,name = safe_pair(vault.decrypt(proposal['bank_text']),request.form.get('merchant',''))
                text_key=vault.index(text)
                owner=vault.index(merchant_key(name))
                merge_into=request.form.get('merge_into','').strip()
                target_key=vault.index(merchant_key(merge_into)) if merge_into else None
                targets=db.execute('SELECT * FROM mappings WHERE merchant_key=? AND active=1 ORDER BY id',(target_key,)).fetchall() if target_key else []
                if merge_into and not targets:
                    abort(409,'Forhandleren findes ikke længere. Genindlæs listen og vælg igen.')
                if targets and owner!=target_key and request.form.get('replace')!='yes':
                    abort(409,'Bekræft, at det nye navn må bruges til alle banktekster hos den valgte forhandler.')
                known = db.execute('SELECT * FROM mappings WHERE bank_key=? AND active=1',(text_key,)).fetchone()
                if known and known['merchant_key'] != owner and request.form.get('replace') != 'yes':
                    abort(409,'Bekræft særskilt, at den eksisterende sammenhæng må erstattes.')
                canonical = db.execute('SELECT merchant FROM mappings WHERE merchant_key=? AND active=1 LIMIT 1',(owner,)).fetchone()
                name = vault.decrypt(canonical['merchant']) if canonical else name
                # A deliberate merge+rename changes the target merchant's known
                # bank texts together. Every changed row gets a distinct feed revision.
                if targets and owner!=target_key:
                    for target in targets:
                        db.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
                        target_revision=db.execute("SELECT value FROM metadata WHERE name='revision'").fetchone()[0]
                        db.execute('UPDATE mappings SET merchant=?,merchant_key=?,revision=? WHERE id=?',
                            (vault.encrypt(name),owner,target_revision,target['id']))
                db.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
                revision = db.execute("SELECT value FROM metadata WHERE name='revision'").fetchone()[0]
                db.execute('''INSERT INTO mappings(bank_key,bank_text,merchant,merchant_key,revision) VALUES (?,?,?,?,?)
                    ON CONFLICT(bank_key) DO UPDATE SET merchant=excluded.merchant,merchant_key=excluded.merchant_key,
                    active=1,revision=excluded.revision''',(text_key,vault.encrypt(text),vault.encrypt(name),owner,revision))
                # All equivalent pending proposals are handled once; conflicting ones remain for review.
                db.execute("UPDATE proposals SET status='merged',reviewed_at=? WHERE bank_key=? AND merchant_key=? AND status='pending'",
                    (timestamp(),text_key,owner))
            db.execute('UPDATE proposals SET status=?,reviewed_at=? WHERE id=?',
                ('merged' if action=='approve' else 'rejected',timestamp(),proposal_id))
            db.execute('INSERT INTO audit(action,proposal_id,created_at) VALUES (?,?,?)',(action,proposal_id,timestamp()))
        return redirect('/')

    @app.post('/proposals/<int:proposal_id>/revoke')
    def revoke(proposal_id):
        with connect(path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT * FROM proposals WHERE id=? AND status='merged'",(proposal_id,)).fetchone()
            if not row:
                abort(404)
            db.execute("UPDATE metadata SET value=value+1 WHERE name='revision'")
            revision = db.execute("SELECT value FROM metadata WHERE name='revision'").fetchone()[0]
            db.execute('UPDATE mappings SET active=0,revision=? WHERE bank_key=?', (revision,row['bank_key']))
            db.execute("UPDATE proposals SET status='rejected',reviewed_at=? WHERE bank_key=? AND status='merged'",(timestamp(),row['bank_key']))
            db.execute('INSERT INTO audit(action,proposal_id,created_at) VALUES (?,?,?)',('revoke',proposal_id,timestamp()))
        return redirect('/?status=merged')

    return app
