import hashlib
import json
import os
import re
import secrets
import threading
import time
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

from cryptography.fernet import Fernet
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from flask import Flask, g, jsonify, redirect, render_template, request, session
from werkzeug.exceptions import HTTPException

from banking import BankError, EnableBanking, account_name, sync_all
from db import CATEGORIES, COLORS, CURRENCIES, cents, connect, initialize
from hub_auth import register_hub_auth


def persistent_key(path, factory):
    if not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(factory())
        except FileExistsError:
            pass
    return path.read_bytes()


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(DATA_DIR=os.environ.get('DATA_DIR', str(Path(__file__).parent/'data')),
                      AUTH_MODE=os.environ.get('AUTH_MODE', 'local'),
                      FJORDHUB_URL=os.environ.get('FJORDHUB_URL', ''),
                      FJORDHUB_API_KEY=os.environ.get('FJORDHUB_API_KEY', ''),
                      SESSION_COOKIE_NAME='fjordbudget_session', PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
                      APP_URL=os.environ.get('APP_URL', 'http://localhost:8060').rstrip('/'),
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax',
                      MAX_CONTENT_LENGTH=16384, TRUSTED_HOSTS=['localhost', '127.0.0.1'])
    if config:
        app.config.update(config)
    data_dir = Path(app.config['DATA_DIR'])
    data_dir.mkdir(parents=True, exist_ok=True)
    app.secret_key = persistent_key(data_dir/'session.key', lambda: secrets.token_bytes(32))
    cipher = Fernet(persistent_key(data_dir/'encryption.key', Fernet.generate_key))
    db_path = data_dir/'budget.sqlite3'
    initialize(db_path)
    credential_store = data_dir/'bank-credentials.enc'
    provider = app.config.get('PROVIDER') or EnableBanking(os.environ.get('ENABLE_BANKING_APP_ID', ''), os.environ.get('ENABLE_BANKING_KEY_FILE', '/run/secrets/enablebanking.pem'), credential_store, cipher)
    app.extensions.update(db_path=db_path, bank_provider=provider, cipher=cipher)
    public_url = register_hub_auth(app, db_path)
    job = {'running': False, 'message': '', 'error': False}
    job_lock = threading.Lock()

    @app.before_request
    def protect_local_app():
        if request.endpoint == 'login':
            return None  # Login form validates its own synchronizer token.
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            expected = session.get('csrf')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not secrets.compare_digest(expected, supplied):
                return jsonify(error='Sessionen er udløbet. Genindlæs siden.'), 403
            origin = request.headers.get('Origin')
            if origin and origin != getattr(g, 'budget_origin', request.host_url.rstrip('/')):
                return jsonify(error='Forespørgslen skal komme fra denne app.'), 403

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        return response

    @app.errorhandler(BankError)
    def bank_error(error):
        return jsonify(error=str(error)), 502

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.description), error.code

    @app.errorhandler(Exception)
    def unexpected(error):
        app.logger.error('Request failed: %s', type(error).__name__)
        return jsonify(error='Der opstod en fejl. Prøv igen.'), 500

    def parameters():
        source = request.args.get('source', 'demo')
        month = request.args.get('month', date.today().strftime('%Y-%m'))
        currency = request.args.get('currency', 'DKK')
        if source not in ('demo', 'live') or currency not in CURRENCIES:
            raise ValueError('Ugyldigt datavalg.')
        if not re.fullmatch(r'\d{4}-\d{2}', month):
            raise ValueError('Ugyldig måned.')
        date.fromisoformat(month+'-01')
        return source, month, currency

    @app.get('/')
    def index():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        session.setdefault('browser', secrets.token_urlsafe(32))
        return render_template('index.html', csrf=session['csrf'], managed=app.config['AUTH_MODE']=='fjordhub')

    @app.get('/privacy')
    def privacy():
        return render_template('legal.html', page='privacy', title='Privatlivspolitik')

    @app.get('/terms')
    def terms():
        return render_template('legal.html', page='terms', title='Brugsvilkår')

    @app.get('/api/health')
    def health():
        with connect(db_path) as db:
            db.execute('SELECT 1').fetchone()
        return jsonify(ok=True)

    @app.get('/api/config')
    def configuration():
        return jsonify(provider_configured=provider.configured, callback_url=public_url()+'/bank/callback',
                       privacy_url=public_url()+'/privacy', terms_url=public_url()+'/terms',
                       categories=CATEGORIES, colors=COLORS, currencies=sorted(CURRENCIES),
                       month=date.today().strftime('%Y-%m'), read_only=True)

    @app.post('/api/bank/credentials')
    def save_bank_credentials():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ValueError('Indsæt app-id og privat API-nøgle.')
        app_id, private_key = data.get('app_id'), data.get('private_key')
        if not isinstance(app_id, str) or not isinstance(private_key, str):
            raise ValueError('Indsæt app-id og privat API-nøgle.')
        try:
            app_id = str(uuid.UUID(app_id.strip()))
        except ValueError:
            raise ValueError('App-id skal være et gyldigt UUID fra Enable Banking.') from None
        try:
            key = serialization.load_pem_private_key(private_key.strip().encode(), password=None)
            if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
                raise ValueError()
        except (ValueError, TypeError, UnsupportedAlgorithm):
            raise ValueError('Indsæt hele den private RSA-nøgle i PEM-format, inklusive BEGIN og END. Nøglen skal være mindst 2048 bit og uden adgangskode.') from None
        normalized = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()).decode()
        encrypted = cipher.encrypt(json.dumps({'app_id': app_id, 'private_key': normalized}).encode())
        # One atomic replacement keeps the ID and key paired across concurrent requests.
        fd, temporary = tempfile.mkstemp(prefix='.bank-credentials-', dir=data_dir)
        try:
            with os.fdopen(fd, 'wb') as output:
                output.write(encrypted)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, credential_store)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return jsonify(ok=True, provider_configured=True)

    @app.get('/api/dashboard')
    def dashboard():
        source, month, currency = parameters()
        with connect(db_path) as db:
            accounts = [dict(r) for r in db.execute('SELECT id,COALESCE(custom_name,name) name,custom_name,bank,last4,currency,balance,balance_type,synced_at FROM accounts WHERE source=? ORDER BY rowid', (source,))]
            rows = db.execute('''SELECT t.category,t.amount FROM transactions t JOIN accounts a ON a.id=t.account_id
                                 WHERE a.source=? AND t.currency=? AND substr(t.booked_on,1,7)=?''', (source, currency, month)).fetchall()
            budgets = {r['category']: r['amount'] for r in db.execute('SELECT category,amount FROM budgets WHERE source=? AND month=? AND currency=?', (source, month, currency))}
            months = [r[0] for r in db.execute('''SELECT DISTINCT substr(t.booked_on,1,7) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.source=? ORDER BY 1 DESC''', (source,))]
            connections = [dict(r) for r in db.execute('SELECT id,bank,valid_until,created_at FROM connections ORDER BY created_at DESC')] if source == 'live' else []
            history = [dict(r) for r in db.execute('''SELECT substr(t.booked_on,1,7) month,
                 SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END) income,
                 SUM(CASE WHEN t.amount<0 THEN -t.amount ELSE 0 END) expenses
                 FROM transactions t JOIN accounts a ON a.id=t.account_id
                 WHERE a.source=? AND t.currency=? AND t.category!='Overførsler'
                 AND substr(t.booked_on,1,7)<=? GROUP BY 1 ORDER BY 1 DESC LIMIT 6''', (source, currency, month))]
        income = sum(r['amount'] for r in rows if r['amount'] > 0 and r['category'] != 'Overførsler')
        expenses = -sum(r['amount'] for r in rows if r['amount'] < 0 and r['category'] != 'Overførsler')
        categories = [{'name': cat, 'color': COLORS[i], 'spent': -sum(r['amount'] for r in rows if r['category'] == cat and r['amount'] < 0),
                       'budget': budgets.get(cat, 0)} for i, cat in enumerate(CATEGORIES[:8])]
        selected = [a for a in accounts if a['currency'] == currency]
        return jsonify(accounts=accounts, connections=connections, income=income, expenses=expenses,
                       balance=sum(a['balance'] for a in selected if a['balance'] is not None), missing_balances=sum(a['balance'] is None for a in selected),
                       budget=sum(budgets.values()), categories=categories, months=months, history=list(reversed(history)), month=month)

    @app.get('/api/transactions')
    def transactions():
        source, month, currency = parameters()
        page = max(1, min(int(request.args.get('page', '1')), 100000))
        account = request.args.get('account', '')
        query = request.args.get('q', '').strip()[:200]
        category = request.args.get('category', '')
        conditions = ['a.source=?', 't.currency=?', 'substr(t.booked_on,1,7)=?']
        values = [source, currency, month]
        if account:
            conditions.append('a.id=?')
            values.append(account)
        if query:
            conditions.append('instr(lower(t.description),lower(?))>0')
            values.append(query)
        if category:
            if category not in CATEGORIES:
                raise ValueError('Ukendt kategori.')
            conditions.append('t.category=?')
            values.append(category)
        where = ' AND '.join(conditions)
        with connect(db_path) as db:
            total = db.execute('SELECT count(*) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE '+where, values).fetchone()[0]
            rows = db.execute('''SELECT t.id,t.booked_on,t.description,t.amount,t.currency,t.category,COALESCE(a.custom_name,a.name) account
                                 FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE '''+where+' ORDER BY t.booked_on DESC,t.id DESC LIMIT 30 OFFSET ?', [*values, (page-1)*30]).fetchall()
        return jsonify(items=[dict(r) for r in rows], total=total, page=page, pages=max(1, (total+29)//30))

    @app.patch('/api/transactions/<int:transaction_id>')
    def update_category(transaction_id):
        category = request.get_json().get('category')
        if category not in CATEGORIES:
            raise ValueError('Ukendt kategori.')
        with connect(db_path) as db:
            result = db.execute('UPDATE transactions SET category=?,category_manual=1 WHERE id=?', (category, transaction_id))
            if not result.rowcount:
                return jsonify(error='Posteringen blev ikke fundet.'), 404
        return jsonify(ok=True)

    @app.put('/api/budgets')
    def save_budgets():
        source, month, currency = parameters()
        amounts = request.get_json().get('amounts')
        if not isinstance(amounts, dict) or set(amounts) != set(CATEGORIES[:8]):
            raise ValueError('Budgettet skal indeholde alle udgiftskategorier.')
        amounts = {cat: cents(value) for cat, value in amounts.items()}
        if any(value < 0 for value in amounts.values()):
            raise ValueError('Et budget kan ikke være negativt.')
        with connect(db_path) as db:
            for cat, value in amounts.items():
                db.execute('INSERT INTO budgets VALUES (?,?,?,?,?) ON CONFLICT(source,month,currency,category) DO UPDATE SET amount=excluded.amount', (source, month, currency, cat, value))
        return jsonify(ok=True)

    @app.get('/api/banks')
    def banks():
        if not provider.configured:
            return jsonify(configured=False, banks=[])
        available = provider.banks()
        return jsonify(configured=True, banks=[{'name': b['name'], 'country': b['country']} for b in available if b['country'] == 'DK' and 'personal' in b.get('psu_types', ['personal'])])

    @app.post('/api/bank/connect')
    def bank_connect():
        name = request.get_json().get('bank')
        available = provider.banks()
        bank = next((b for b in available if b['name'] == name and b['country'] == 'DK'), None)
        if not bank:
            raise ValueError('Vælg en tilgængelig bank.')
        state = secrets.token_urlsafe(32)
        browser = session.get('browser')
        if not browser:
            return jsonify(error='Genindlæs siden, og prøv igen.'), 403
        valid_until = (datetime.now(timezone.utc)+timedelta(days=90)).isoformat()
        result = provider.request('POST', '/auth', json={'access': {'valid_until': valid_until}, 'aspsp': {'name': name, 'country': 'DK'},
                                  'state': state, 'redirect_url': public_url()+'/bank/callback', 'psu_type': 'personal', 'language': 'da'})
        url = urlparse(result['url'])
        if (url.scheme != 'https'
                or url.hostname not in {'auth.enablebanking.com', 'tilisy.enablebanking.com'}
                or url.port not in (None, 443)
                or url.username is not None or url.password is not None):
            raise BankError('Banktjenesten gav en uventet godkendelsesadresse.')
        with connect(db_path) as db:
            db.execute('DELETE FROM oauth_states WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO oauth_states VALUES (?,?,?,?)', (hashlib.sha256(state.encode()).hexdigest(), hashlib.sha256(browser.encode()).hexdigest(), name, time.time()+900))
        return jsonify(url=result['url'])

    @app.get('/bank/callback')
    def bank_callback():
        state = request.args.get('state', '')
        browser = session.get('browser', '')
        # Atomically consume state before exchanging a one-time code.
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            pending = db.execute('SELECT * FROM oauth_states WHERE state_hash=? AND browser_hash=? AND expires>?',
                                 (hashlib.sha256(state.encode()).hexdigest(), hashlib.sha256(browser.encode()).hexdigest(), time.time())).fetchone()
            if not pending or not browser:
                return redirect('/?bank_result=invalid')
            db.execute('DELETE FROM oauth_states WHERE state_hash=?', (pending['state_hash'],))
        if request.args.get('error') or not request.args.get('code'):
            return redirect('/?bank_result=cancelled')
        try:
            result = provider.request('POST', '/sessions', json={'code': request.args['code']})
            connection_id = str(uuid.uuid4())
            accounts = result['accounts']
            valid_until = result['access']['valid_until']
            with connect(db_path) as db:
                db.execute('INSERT INTO connections VALUES (?,?,?,?,?)', (connection_id, pending['bank'], cipher.encrypt(result['session_id'].encode()).decode(), valid_until, datetime.now(timezone.utc).isoformat()))
                for account in accounts:
                    identity = account.get('identification_hash')
                    iban = (account.get('account_id') or {}).get('iban', '')
                    identity = identity or iban
                    if not identity:
                        raise BankError('Banken gav ingen stabil identifikation af en konto.')
                    account_id = hashlib.sha256((pending['bank']+':'+identity).encode()).hexdigest()
                    currency = account.get('currency', 'DKK')
                    if currency not in CURRENCIES:
                        raise BankError('En kontovaluta understøttes endnu ikke.')
                    db.execute('''INSERT INTO accounts(id,source,connection_id,remote_id,name,bank,last4,currency)
                      VALUES (?,'live',?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET connection_id=excluded.connection_id,
                      remote_id=excluded.remote_id,name=excluded.name,currency=excluded.currency''',
                               (account_id, connection_id, cipher.encrypt(account['uid'].encode()).decode(), account_name(account), pending['bank'], iban[-4:], currency))
            return redirect('/?bank_result=connected')
        except (BankError, KeyError, ValueError):
            return redirect('/?bank_result=failed')

    @app.put('/api/accounts/setup')
    def setup_accounts():
        body = request.get_json()
        names = body.get('names') if isinstance(body, dict) else None
        if not isinstance(names, dict) or not names:
            raise ValueError('Udfyld kontonavnene.')
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            for account_id, name in names.items():
                if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 100
                        or any(ord(char) < 32 for char in name)):
                    raise ValueError('Hver konto skal have et navn på 1–100 tegn uden linjeskift.')
                account = db.execute("SELECT custom_name FROM accounts WHERE id=? AND source='live'", (account_id,)).fetchone()
                if account is None:
                    raise ValueError('En af kontiene findes ikke længere. Genindlæs siden.')
                if not account['custom_name']:
                    db.execute('UPDATE accounts SET custom_name=? WHERE id=?', (name.strip(), account_id))
        return jsonify(ok=True)

    @app.put('/api/accounts/<account_id>/name')
    def rename_account(account_id):
        body = request.get_json()
        name = body.get('name') if isinstance(body, dict) else None
        if not isinstance(name, str) or len(name.strip()) > 100:
            raise ValueError('Kontonavnet må højst være 100 tegn.')
        name = name.strip()
        if any(ord(char) < 32 for char in name):
            raise ValueError('Kontonavnet må ikke indeholde linjeskift eller kontroltegn.')
        with connect(db_path) as db:
            changed = db.execute('UPDATE accounts SET custom_name=? WHERE id=?', (name or None, account_id))
            if not changed.rowcount:
                return jsonify(error='Kontoen findes ikke.'), 404
        return jsonify(ok=True)

    @app.get('/api/sync')
    def sync_status():
        with job_lock:
            return jsonify(dict(job))

    @app.post('/api/sync')
    def sync():
        with job_lock:
            if job['running']:
                return jsonify(error='En synkronisering kører allerede.'), 409
            job.update(running=True, message='Kontakter banken …', error=False)

        def update(message):
            with job_lock:
                job['message'] = message

        def run():
            try:
                message = sync_all(db_path, provider, cipher, update)
                with job_lock:
                    job.update(message=message, error=False)
            except BankError as error:
                with job_lock:
                    job.update(message=str(error), error=True)
            except Exception as error:
                app.logger.error('Sync failed: %s', type(error).__name__)
                with job_lock:
                    job.update(message='Synkroniseringen fejlede. Eksisterende posteringer er bevaret.', error=True)
            finally:
                with job_lock:
                    job['running'] = False
        threading.Thread(target=run, daemon=True).start()
        return jsonify(ok=True), 202

    @app.delete('/api/connections/<connection_id>')
    def disconnect(connection_id):
        with job_lock:
            if job['running']:
                return jsonify(error='Vent, til synkroniseringen er færdig.'), 409
        with connect(db_path) as db:
            connection = db.execute('SELECT * FROM connections WHERE id=?', (connection_id,)).fetchone()
        if not connection:
            return jsonify(error='Bankforbindelsen blev ikke fundet.'), 404
        token = cipher.decrypt(connection['session_token'].encode()).decode()
        provider.request('DELETE', '/sessions/'+token)
        with connect(db_path) as db:
            db.execute('UPDATE accounts SET connection_id=NULL,remote_id=NULL WHERE connection_id=?', (connection_id,))
            db.execute('DELETE FROM connections WHERE id=?', (connection_id,))
        return jsonify(ok=True)

    return app
