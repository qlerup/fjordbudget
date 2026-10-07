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
from db import CATEGORIES, COLORS, CURRENCIES, cents, connect, initialize, transaction_title, transaction_rule_title, category_list, budget_category_list
from hub_auth import register_hub_auth
from insights import build_insights


def merchant_key(value):
    return re.sub(r"[\s'’`´.\-]+", '', transaction_title(value))


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
                      AUTO_SYNC_INTERVAL_SECONDS=os.environ.get('AUTO_SYNC_INTERVAL_SECONDS', '1800'),
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
    job = {'running': False, 'message': '', 'error': False, 'automatic': False, 'completed_at': None, 'history': None}
    job_lock = threading.Lock()
    try:
        auto_sync_interval = max(0, int(app.config['AUTO_SYNC_INTERVAL_SECONDS']))
    except (TypeError, ValueError):
        auto_sync_interval = 1800
    app.extensions['auto_sync_interval_seconds'] = auto_sync_interval

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

    def stored_categories():
        with connect(db_path) as db:
            return category_list(db)

    def stored_budget_categories():
        with connect(db_path) as db:
            return budget_category_list(db)

    def merchant_items(db, source):
        rows = db.execute('''SELECT merchant,COUNT(*) uses FROM (
                               SELECT t.merchant merchant FROM transactions t
                               JOIN accounts a ON a.id=t.account_id
                               WHERE a.source=? AND a.included=1 AND a.selection_pending=0
                                 AND t.merchant IS NOT NULL AND trim(t.merchant)!=''
                               UNION ALL
                               SELECT merchant FROM merchant_rules WHERE source=?
                             ) GROUP BY merchant''', (source, source)).fetchall()
        preferences = {row['merchant_key']: bool(row['adjustable']) for row in db.execute(
            'SELECT merchant_key,adjustable FROM merchant_preferences WHERE source=?', (source,))}
        merged = {}
        for row in rows:
            key = merchant_key(row['merchant'])
            uses = int(row['uses'])
            current = merged.get(key)
            if current is None:
                merged[key] = {'name': row['merchant'], 'uses': uses, '_best': uses}
            else:
                current['uses'] += uses
                if uses > current['_best']:
                    current['name'], current['_best'] = row['merchant'], uses
        items = [{'name': item['name'], 'uses': item['uses'],
                  'adjustable': preferences.get(key, True)}
                 for key, item in merged.items()]
        return sorted(items, key=lambda item: (-item['uses'], item['name'].casefold()))

    def merchant_library_items(db, source):
        examples = {row['title']: row['display_text'] for row in db.execute(
            '''SELECT transaction_title(t.description) title,MIN(t.description) display_text
               FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE a.source=? GROUP BY transaction_title(t.description)''', (source,))}
        preferences = {row['merchant_key']: bool(row['adjustable']) for row in db.execute(
            'SELECT merchant_key,adjustable FROM merchant_preferences WHERE source=?', (source,))}
        grouped = {}

        def ensure(name, weight=0):
            key = merchant_key(name)
            item = grouped.get(key)
            if item is None:
                item = {'name': name, 'transactions': 0, 'rules': [], '_best': weight,
                        'adjustable': preferences.get(key, True)}
                grouped[key] = item
            elif weight > item['_best']:
                item['name'], item['_best'] = name, weight
            return item

        for row in db.execute(
            '''SELECT t.merchant,COUNT(*) transactions
               FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE a.source=? AND t.merchant IS NOT NULL AND trim(t.merchant)!=''
               GROUP BY t.merchant''', (source,)):
            item = ensure(row['merchant'], int(row['transactions']))
            item['transactions'] += int(row['transactions'])

        for row in db.execute('SELECT title,merchant FROM merchant_rules WHERE source=? ORDER BY merchant,title', (source,)):
            item = ensure(row['merchant'])
            item['rules'].append({
                'title': row['title'],
                'bank_text': examples.get(row['title'], row['title']),
            })

        result = []
        for item in grouped.values():
            item.pop('_best', None)
            item['rules'].sort(key=lambda rule: rule['bank_text'].casefold())
            item['rule_count'] = len(item['rules'])
            result.append(item)
        return sorted(result, key=lambda item: item['name'].casefold())

    @app.route('/api/merchant-library', methods=['GET', 'PATCH', 'DELETE'])
    def merchant_library():
        source = request.args.get('source', 'demo')
        if source not in ('demo', 'live'):
            raise ValueError('Ugyldig datakilde.')
        if request.method == 'GET':
            with connect(db_path) as db:
                return jsonify(items=merchant_library_items(db, source))

        body = request.get_json(silent=True)
        merchant = body.get('merchant') if isinstance(body, dict) else None
        title = body.get('title') if isinstance(body, dict) else None

        if request.method == 'PATCH':
            adjustable = body.get('adjustable') if isinstance(body, dict) else None
            if not isinstance(merchant, str) or not 1 <= len(merchant.strip()) <= 100:
                raise ValueError('Ugyldig forhandler.')
            if type(adjustable) is not bool:
                raise ValueError('Ugyldigt valg for fast udgift.')
            target = merchant_key(merchant.strip())
            with connect(db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                canonical = next((item['name'] for item in merchant_items(db, source)
                                  if merchant_key(item['name']) == target), None)
                if canonical is None:
                    return jsonify(error='Forhandleren findes ikke længere.'), 404
                db.execute('''INSERT INTO merchant_preferences(source,merchant_key,name,adjustable)
                              VALUES (?,?,?,?)
                              ON CONFLICT(source,merchant_key) DO UPDATE SET
                                name=excluded.name,adjustable=excluded.adjustable''',
                           (source, target, canonical, int(adjustable)))
            return jsonify(ok=True, merchant=canonical, adjustable=adjustable)

        if bool(merchant) == bool(title):
            raise ValueError('Vælg enten en forhandler eller én gemt banktekst.')

        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            if title:
                if not isinstance(title, str) or not 1 <= len(title) <= 1000:
                    raise ValueError('Ugyldig banktekst.')
                removed = db.execute('DELETE FROM merchant_rules WHERE source=? AND title=?', (source, title))
                if not removed.rowcount:
                    return jsonify(error='Den gemte banktekst findes ikke længere.'), 404
                return jsonify(ok=True, deleted_rules=removed.rowcount, cleared_transactions=0)

            if not isinstance(merchant, str) or not 1 <= len(merchant.strip()) <= 100:
                raise ValueError('Ugyldig forhandler.')
            target = merchant_key(merchant.strip())
            rules = [row for row in db.execute('SELECT title,merchant FROM merchant_rules WHERE source=?', (source,))
                     if merchant_key(row['merchant']) == target]
            transactions = [row for row in db.execute(
                '''SELECT t.id,t.merchant FROM transactions t JOIN accounts a ON a.id=t.account_id
                   WHERE a.source=? AND t.merchant IS NOT NULL AND trim(t.merchant)!='' ''', (source,))
                            if merchant_key(row['merchant']) == target]
            if not rules and not transactions:
                return jsonify(error='Forhandleren findes ikke længere.'), 404
            for row in rules:
                db.execute('DELETE FROM merchant_rules WHERE source=? AND title=?', (source, row['title']))
            for row in transactions:
                db.execute('UPDATE transactions SET merchant=NULL,merchant_manual=0 WHERE id=?', (row['id'],))
            db.execute('DELETE FROM merchant_preferences WHERE source=? AND merchant_key=?', (source, target))
        return jsonify(ok=True, deleted_rules=len(rules), cleared_transactions=len(transactions))

    @app.route('/api/categories', methods=['GET', 'POST', 'PATCH', 'DELETE'])
    def manage_categories():
        if request.method == 'GET':
            return jsonify(items=stored_categories(), budget_categories=stored_budget_categories())
        body = request.get_json()
        name = body.get('name') if isinstance(body, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c)<32 for c in name):
            raise ValueError('Kategorinavnet skal være på 1–60 tegn uden linjeskift.')
        name = name.strip()
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            categories = category_list(db)
            if request.method == 'POST':
                if any(transaction_title(c['name']) == transaction_title(name) for c in categories):
                    raise ValueError('Kategorien findes allerede.')
                db.execute('INSERT INTO categories(name,color,protected) VALUES (?,?,0)', (name,COLORS[len(categories)%len(COLORS)]))
            elif request.method == 'PATCH':
                current = next((c for c in categories if c['name']==name), None)
                if current is None:
                    raise ValueError('Kategorien findes ikke.')
                updates, values = [], []
                rename_to = None
                if 'new_name' in body:
                    new_name = body.get('new_name')
                    if (not isinstance(new_name, str) or not 1 <= len(new_name.strip()) <= 60
                            or any(ord(c) < 32 for c in new_name)):
                        raise ValueError('Det nye kategorinavn skal være på 1–60 tegn uden linjeskift.')
                    if current['protected']:
                        raise ValueError('Faste kategorier kan ikke omdøbes.')
                    new_name = new_name.strip()
                    if any(c['name'] != name and transaction_title(c['name']) == transaction_title(new_name) for c in categories):
                        raise ValueError('Kategorien findes allerede.')
                    rename_to = new_name
                    updates.append('name=?')
                    values.append(rename_to)
                if 'budget_category' in body:
                    target = body.get('budget_category')
                    if target is not None and target not in [c['name'] for c in budget_category_list(db)]:
                        raise ValueError('Vælg en gyldig budgetkategori eller ingen.')
                    updates.append('budget_category=?')
                    values.append(target)
                if 'requires_merchant' in body:
                    requires_merchant = body.get('requires_merchant')
                    if type(requires_merchant) is not bool:
                        raise ValueError('Valget for forhandlerkrav er ugyldigt.')
                    updates.append('requires_merchant=?')
                    values.append(int(requires_merchant))
                if not updates:
                    raise ValueError('Der er ingen kategoriindstillinger at gemme.')
                db.execute('UPDATE categories SET '+','.join(updates)+' WHERE name=?', (*values,name))
                if rename_to and rename_to != name:
                    db.execute('UPDATE transactions SET category=? WHERE category=?', (rename_to,name))
                    db.execute('UPDATE category_rules SET category=? WHERE category=?', (rename_to,name))
            else:
                current = next((c for c in categories if c['name']==name),None)
                if not current or current['protected']:
                    raise ValueError('Denne kategori kan ikke slettes.')
                replacement = body.get('replacement')
                if replacement == name or replacement not in [c['name'] for c in categories if c['name'] not in ('Indkomst','Overførsler')]:
                    raise ValueError('Vælg en anden udgiftskategori til posteringer.')
                db.execute('UPDATE transactions SET category=? WHERE category=?',(replacement,name))
                db.execute('UPDATE category_rules SET category=? WHERE category=?',(replacement,name))
                db.execute('DELETE FROM categories WHERE name=?',(name,))
        return jsonify(ok=True,items=stored_categories())

    @app.route('/api/budget-categories', methods=['POST', 'DELETE'])
    def manage_budget_categories():
        body = request.get_json()
        name = body.get('name') if isinstance(body, dict) else None
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 60 or any(ord(c) < 32 for c in name):
            raise ValueError('Kategorinavnet skal være på 1–60 tegn uden linjeskift.')
        name = name.strip()
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            categories = budget_category_list(db)
            if request.method == 'POST':
                if any(transaction_title(c['name']) == transaction_title(name) for c in categories):
                    raise ValueError('Budgetkategorien findes allerede.')
                db.execute('INSERT INTO budget_categories(name,color) VALUES (?,?)',
                           (name, COLORS[len(categories) % len(COLORS)]))
            else:
                if name not in [c['name'] for c in categories]:
                    raise ValueError('Budgetkategorien findes ikke.')
                db.execute('UPDATE categories SET budget_category=NULL WHERE budget_category=?', (name,))
                db.execute('DELETE FROM budgets WHERE category=?', (name,))
                db.execute('DELETE FROM budget_categories WHERE name=?', (name,))
        return jsonify(ok=True)

    @app.route('/api/savings-goals', methods=['GET', 'POST'])
    @app.route('/api/savings-goals/<int:goal_id>', methods=['PUT', 'DELETE'])
    def savings_goals(goal_id=None):
        source, _, currency = parameters()
        if request.method == 'GET':
            raw_from = str(request.args.get('from') or '').strip()
            raw_to = str(request.args.get('to') or '').strip()
            if bool(raw_from) != bool(raw_to):
                raise ValueError('Vælg både fra- og til-dato.')
            if raw_from:
                try:
                    period_start = date.fromisoformat(raw_from)
                    period_end = date.fromisoformat(raw_to)
                except ValueError:
                    raise ValueError('Vælg en gyldig fra- og til-dato.') from None
                if period_start > period_end:
                    raise ValueError('Fra-dato skal være før eller samme dag som til-dato.')
            else:
                first_this_month = date.today().replace(day=1)
                period_end = first_this_month - timedelta(days=1)
                period_start = period_end.replace(day=1)
            result = build_insights(
                db_path, source, currency,
                period_start=period_start, period_end=period_end,
            )
            with connect(db_path) as db:
                accounts = [dict(row) for row in db.execute(
                    '''SELECT id,COALESCE(custom_name,name) name,bank,last4,currency,balance,synced_at,included
                       FROM accounts
                       WHERE source=? AND currency=? AND selection_pending=0
                       ORDER BY included DESC,bank,COALESCE(custom_name,name),id''',
                    (source, currency))]
            return jsonify(items=result['goals'], profile=result['profile'], accounts=accounts)
        if request.method != 'DELETE':
            body = request.get_json(silent=True)
            name = body.get('name') if isinstance(body, dict) else None
            if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80 or any(ord(c)<32 for c in name):
                raise ValueError('Navnet skal være på 1–80 tegn uden linjeskift.')
            amount = cents(body.get('target_amount'))
            saved_amount = cents(body.get('saved_amount', 0))
            account_id = body.get('account_id')
            if account_id in ('', None):
                account_id = None
            elif not isinstance(account_id, str) or not 1 <= len(account_id) <= 200 or any(ord(c)<32 for c in account_id):
                raise ValueError('Den valgte konto er ugyldig.')
            if amount <= 0:
                raise ValueError('Opsparingsmålet skal være større end 0.')
            if saved_amount < 0 or saved_amount > amount:
                raise ValueError('Allerede opsparet skal være mellem 0 og målbeløbet.')
            featured = body.get('featured', False)
            if type(featured) is not bool:
                raise ValueError('Ugyldigt valg for visning på overblikket.')
            deadline = body.get('deadline')
            if not isinstance(deadline, str) or not re.fullmatch(r'\d{4}-\d{2}(?:-\d{2})?', deadline):
                raise ValueError('Vælg en gyldig deadline.')
            try:
                date.fromisoformat(deadline + '-01' if len(deadline) == 7 else deadline)
            except ValueError:
                raise ValueError('Vælg en gyldig deadline.') from None
            deadline = deadline[:7]
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            if goal_id is not None and not db.execute(
                'SELECT 1 FROM savings_goals WHERE id=? AND source=? AND currency=?',
                (goal_id, source, currency)).fetchone():
                return jsonify(error='Opsparingsmålet findes ikke.'), 404
            if request.method != 'DELETE' and account_id is not None:
                linked_account = db.execute(
                    '''SELECT 1 FROM accounts
                       WHERE id=? AND source=? AND currency=? AND selection_pending=0''',
                    (account_id, source, currency)).fetchone()
                if linked_account is None:
                    raise ValueError('Vælg en konto fra den aktuelle datakilde og valuta.')
            if request.method == 'POST':
                existing = db.execute('SELECT 1 FROM savings_goals WHERE source=? AND currency=? LIMIT 1', (source, currency)).fetchone()
                has_featured = db.execute('SELECT 1 FROM savings_goals WHERE source=? AND currency=? AND featured=1 LIMIT 1', (source, currency)).fetchone()
                make_featured = bool(featured or not existing or not has_featured)
                if make_featured:
                    db.execute('UPDATE savings_goals SET featured=0 WHERE source=? AND currency=?', (source, currency))
                goal_id = db.execute('''INSERT INTO savings_goals(source,currency,name,target_amount,saved_amount,deadline,featured,account_id)
                                    VALUES (?,?,?,?,?,?,?,?)''',
                                    (source, currency, name.strip(), amount, saved_amount, deadline, int(make_featured), account_id)).lastrowid
            elif request.method == 'PUT':
                if featured:
                    db.execute('UPDATE savings_goals SET featured=0 WHERE source=? AND currency=?', (source, currency))
                db.execute('UPDATE savings_goals SET name=?,target_amount=?,saved_amount=?,deadline=?,featured=?,account_id=? WHERE id=?',
                           (name.strip(), amount, saved_amount, deadline, int(featured), account_id, goal_id))
                if not db.execute('SELECT 1 FROM savings_goals WHERE source=? AND currency=? AND featured=1 LIMIT 1', (source, currency)).fetchone():
                    fallback = db.execute('SELECT id FROM savings_goals WHERE source=? AND currency=? ORDER BY deadline,id LIMIT 1', (source, currency)).fetchone()
                    if fallback:
                        db.execute('UPDATE savings_goals SET featured=1 WHERE id=?', (fallback['id'],))
            else:
                db.execute('DELETE FROM savings_goals WHERE id=?', (goal_id,))
                if not db.execute('SELECT 1 FROM savings_goals WHERE source=? AND currency=? AND featured=1 LIMIT 1', (source, currency)).fetchone():
                    fallback = db.execute('SELECT id FROM savings_goals WHERE source=? AND currency=? ORDER BY deadline,id LIMIT 1', (source, currency)).fetchone()
                    if fallback:
                        db.execute('UPDATE savings_goals SET featured=1 WHERE id=?', (fallback['id'],))
        return jsonify(ok=True, id=goal_id)
    @app.get('/api/config')
    def configuration():
        categories = stored_categories()
        with connect(db_path) as db:
            has_bank_connections = db.execute('SELECT 1 FROM connections LIMIT 1').fetchone() is not None
        return jsonify(has_bank_connections=has_bank_connections, provider_configured=provider.configured, callback_url=public_url()+'/bank/callback',
                       privacy_url=public_url()+'/privacy', terms_url=public_url()+'/terms',
                       categories=[c['name'] for c in categories], colors=[c['color'] for c in categories], currencies=sorted(CURRENCIES),
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
            has_bank_connections = db.execute('SELECT 1 FROM connections LIMIT 1').fetchone() is not None
            accounts = [dict(r) for r in db.execute('''SELECT id,COALESCE(custom_name,name) name,custom_name,bank,last4,currency,balance,balance_type,synced_at
                                                        FROM accounts WHERE source=? AND included=1 AND selection_pending=0 ORDER BY rowid''', (source,))]
            hidden_account_count = db.execute('SELECT count(*) FROM accounts WHERE source=? AND (included=0 OR selection_pending=1)', (source,)).fetchone()[0]
            rows = db.execute('''SELECT t.category,t.amount,t.merchant,c.budget_category,COALESCE(c.requires_merchant,1) requires_merchant
                                 FROM transactions t LEFT JOIN categories c ON c.name=t.category JOIN accounts a ON a.id=t.account_id
                                 WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=? AND substr(t.booked_on,1,7)=?''', (source, currency, month)).fetchall()
            budgets = {r['category']: r['amount'] for r in db.execute('SELECT category,amount FROM budgets WHERE source=? AND month=? AND currency=?', (source, month, currency))}
            months = [r[0] for r in db.execute('''SELECT DISTINCT substr(t.booked_on,1,7) FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE a.source=? AND a.included=1 AND a.selection_pending=0 ORDER BY 1 DESC''', (source,))]
            connections = [dict(r) for r in db.execute('SELECT id,bank,valid_until,created_at FROM connections ORDER BY created_at DESC')] if source == 'live' else []
            history = [dict(r) for r in db.execute('''SELECT substr(t.booked_on,1,7) month,
                 SUM(CASE WHEN t.amount>0 THEN t.amount ELSE 0 END) income,
                 SUM(CASE WHEN t.amount<0 THEN -t.amount ELSE 0 END) expenses
                 FROM transactions t JOIN accounts a ON a.id=t.account_id
                 WHERE a.source=? AND a.included=1 AND a.selection_pending=0 AND t.currency=? AND t.category!='Overførsler'
                 AND substr(t.booked_on,1,7)<=? GROUP BY 1 ORDER BY 1 DESC LIMIT 6''', (source, currency, month))]
        income = sum(r['amount'] for r in rows if r['amount'] > 0 and r['category'] != 'Overførsler')
        expenses = -sum(r['amount'] for r in rows if r['amount'] < 0 and r['category'] != 'Overførsler')
        categories = [{'name': cat['name'], 'color': cat['color'], 'spent': -sum(r['amount'] for r in rows if r['budget_category'] == cat['name'] and r['amount'] < 0 and r['category'] != 'Overførsler'),
                       'budget': budgets.get(cat['name'], 0)} for cat in stored_budget_categories()]
        selected = [a for a in accounts if a['currency'] == currency]
        incomplete_transactions = sum(
            1 for row in rows
            if row['amount'] < 0 and (
                not str(row['category'] or '').strip()
                or (bool(row['requires_merchant']) and not str(row['merchant'] or '').strip())
            )
        )
        goal_insights = build_insights(db_path, source, currency)
        return jsonify(has_bank_connections=has_bank_connections, accounts=accounts, connections=connections, income=income, expenses=expenses,
                       balance=sum(a['balance'] for a in selected if a['balance'] is not None), missing_balances=sum(a['balance'] is None for a in selected),
                       budget=sum(budgets.values()), budget_spent=sum(c['spent'] for c in categories), categories=categories, months=months,
                       history=list(reversed(history)), month=month, featured_goal=goal_insights['featured_goal'],
                       hidden_account_count=hidden_account_count, incomplete_transactions=incomplete_transactions)

    @app.get('/api/transactions')
    def transactions():
        source, month, currency = parameters()
        page = max(1, min(int(request.args.get('page', '1')), 100000))
        per_page = max(1, min(int(request.args.get('per_page', '30')), 200))
        account = request.args.get('account', '')
        query = request.args.get('q', '').strip()[:200]
        category = request.args.get('category', '')
        missing = request.args.get('missing') == '1'
        conditions = ['a.source=?', 'a.included=1', 'a.selection_pending=0', 't.currency=?', 'substr(t.booked_on,1,7)=?']
        values = [source, currency, month]
        if account:
            conditions.append('a.id=?')
            values.append(account)
        if query:
            conditions.append("(instr(lower(t.description),lower(?))>0 OR instr(lower(COALESCE(t.merchant,'')),lower(?))>0)")
            values.extend([query, query])
        if category:
            if category not in [c['name'] for c in stored_categories()]:
                raise ValueError('Ukendt kategori.')
            conditions.append('t.category=?')
            values.append(category)
        if missing:
            conditions.append("""t.amount<0 AND (
                trim(COALESCE(t.category,''))=''
                OR (COALESCE(c.requires_merchant,1)=1 AND trim(COALESCE(t.merchant,''))='')
            )""")
        where = ' AND '.join(conditions)
        with connect(db_path) as db:
            total = db.execute('''SELECT count(*) FROM transactions t
                                  JOIN accounts a ON a.id=t.account_id
                                  LEFT JOIN categories c ON c.name=t.category
                                  WHERE '''+where, values).fetchone()[0]
            rows = db.execute('''SELECT t.id,t.booked_on,t.description,t.amount,t.currency,t.category,t.category_manual,
                                        t.merchant,t.merchant_manual,COALESCE(c.requires_merchant,1) requires_merchant,
                                        COALESCE(a.custom_name,a.name) account
                                 FROM transactions t JOIN accounts a ON a.id=t.account_id
                                 LEFT JOIN categories c ON c.name=t.category
                                 WHERE '''+where+' ORDER BY t.booked_on DESC,t.id DESC LIMIT ? OFFSET ?', [*values, per_page, (page-1)*per_page]).fetchall()
        return jsonify(items=[dict(r) for r in rows], total=total, page=page, pages=max(1, (total+per_page-1)//per_page))

    @app.get('/api/merchants')
    def merchants():
        source = request.args.get('source', 'demo')
        if source not in ('demo', 'live'):
            raise ValueError('Ugyldig datakilde.')
        with connect(db_path) as db:
            items = merchant_items(db, source)
        return jsonify(items=items)

    @app.get('/api/accounts/manage')
    def manage_accounts_list():
        pending_only = request.args.get('pending') == '1'
        with connect(db_path) as db:
            where = "source='live'" + (" AND selection_pending=1" if pending_only else "")
            rows = [dict(row) for row in db.execute(f'''SELECT id,COALESCE(custom_name,name) name,custom_name,bank,last4,currency,
                                                              balance,synced_at,included,selection_pending,
                                                              CASE WHEN connection_id IS NULL THEN 0 ELSE 1 END connected
                                                       FROM accounts WHERE {where}
                                                       ORDER BY bank,COALESCE(custom_name,name),id''')]
        return jsonify(items=rows)

    @app.put('/api/accounts/manage')
    def manage_accounts_save():
        body = request.get_json(silent=True)
        included = body.get('included') if isinstance(body, dict) else None
        if not isinstance(included, dict) or not included:
            raise ValueError('Vælg hvilke konti der skal være med.')
        if any(not isinstance(account_id, str) or type(value) is not bool for account_id, value in included.items()):
            raise ValueError('Ugyldigt kontovalg.')
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            rows = {row['id'] for row in db.execute("SELECT id FROM accounts WHERE source='live'")}
            if not set(included).issubset(rows):
                raise ValueError('En af kontiene findes ikke længere. Genindlæs siden.')
            for account_id, value in included.items():
                db.execute('UPDATE accounts SET included=?,selection_pending=0 WHERE id=?', (int(value), account_id))
            active = db.execute("SELECT count(*) FROM accounts WHERE source='live' AND included=1 AND selection_pending=0").fetchone()[0]
        return jsonify(ok=True, active=active)
    @app.patch('/api/transactions/<int:transaction_id>')
    def update_category(transaction_id):
        category = request.get_json().get('category')
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            if category not in [c['name'] for c in category_list(db)]:
                raise ValueError('Ukendt kategori.')
            row = db.execute('SELECT t.description,a.source FROM transactions t JOIN accounts a ON a.id=t.account_id WHERE t.id=?', (transaction_id,)).fetchone()
            if row is None:
                return jsonify(error='Posteringen blev ikke fundet.'), 404
            title = transaction_title(row['description'])
            signature = transaction_rule_title(row['description'])
            db.execute('''INSERT INTO category_rules(source,title,category) VALUES (?,?,?)
              ON CONFLICT(source,title) DO UPDATE SET category=excluded.category''', (row['source'], title, category))
            result = db.execute('''UPDATE transactions SET category=?,category_manual=1
              WHERE transaction_rule_title(description)=?
                AND account_id IN (SELECT id FROM accounts WHERE source=?)''',
              (category, signature, row['source']))
            updated = result.rowcount
        return jsonify(ok=True, updated=updated, rule_saved=True)

    @app.patch('/api/transactions/<int:transaction_id>/merchant')
    def update_merchant(transaction_id):
        body = request.get_json(silent=True)
        merchant = body.get('merchant') if isinstance(body, dict) else None
        remember = body.get('remember', True) if isinstance(body, dict) else True
        adjustable = body.get('adjustable') if isinstance(body, dict) and 'adjustable' in body else None
        if not isinstance(merchant, str) or len(merchant.strip()) > 100 or any(ord(c) < 32 for c in merchant):
            raise ValueError('Forhandlernavnet skal være på højst 100 tegn uden linjeskift.')
        if type(remember) is not bool:
            raise ValueError('Ugyldigt valg for huskeregel.')
        if adjustable is not None and type(adjustable) is not bool:
            raise ValueError('Ugyldigt valg for fast udgift.')
        merchant = merchant.strip() or None
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('''SELECT t.description,a.source FROM transactions t
                                JOIN accounts a ON a.id=t.account_id WHERE t.id=?''', (transaction_id,)).fetchone()
            if row is None:
                return jsonify(error='Posteringen blev ikke fundet.'), 404
            title = transaction_title(row['description'])
            signature = transaction_rule_title(row['description'])
            if merchant:
                key = merchant_key(merchant)
                canonical = next((item['name'] for item in merchant_items(db, row['source'])
                                  if merchant_key(item['name']) == key), None)
                is_new_merchant = canonical is None
                merchant = canonical or merchant
                if adjustable is not None and is_new_merchant:
                    db.execute('''INSERT INTO merchant_preferences(source,merchant_key,name,adjustable)
                                  VALUES (?,?,?,?)
                                  ON CONFLICT(source,merchant_key) DO UPDATE SET
                                    name=excluded.name,adjustable=excluded.adjustable''',
                               (row['source'], key, merchant, int(adjustable)))
            if remember:
                if merchant:
                    db.execute('''INSERT INTO merchant_rules(source,title,merchant) VALUES (?,?,?)
                                  ON CONFLICT(source,title) DO UPDATE SET merchant=excluded.merchant''',
                               (row['source'], title, merchant))
                else:
                    db.execute('DELETE FROM merchant_rules WHERE source=? AND title=?', (row['source'], title))
                result = db.execute('''UPDATE transactions SET merchant=?,merchant_manual=1
                                       WHERE transaction_rule_title(description)=?
                                         AND account_id IN (SELECT id FROM accounts WHERE source=?)''',
                                    (merchant, signature, row['source']))
            else:
                result = db.execute('UPDATE transactions SET merchant=?,merchant_manual=1 WHERE id=?',
                                    (merchant, transaction_id))
            updated = result.rowcount
        return jsonify(ok=True, updated=updated, rule_saved=remember)

    @app.put('/api/budgets')
    def save_budgets():
        source, month, currency = parameters()
        amounts = request.get_json().get('amounts')
        if not isinstance(amounts, dict) or set(amounts) != {c['name'] for c in stored_budget_categories()}:
            raise ValueError('Budgettet skal indeholde alle udgiftskategorier.')
        amounts = {cat: cents(value) for cat, value in amounts.items()}
        if any(value < 0 for value in amounts.values()):
            raise ValueError('Et budget kan ikke være negativt.')
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            if set(amounts) != {c['name'] for c in budget_category_list(db)}:
                raise ValueError('Kategorierne er ændret. Genindlæs siden og prøv igen.')
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
                    db.execute('''INSERT INTO accounts(id,source,connection_id,remote_id,name,bank,last4,currency,included,selection_pending)
                      VALUES (?,'live',?,?,?,?,?,?,0,1) ON CONFLICT(id) DO UPDATE SET connection_id=excluded.connection_id,
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
                account = db.execute("""SELECT custom_name FROM accounts
                                        WHERE id=? AND source='live' AND included=1 AND selection_pending=0""",
                                     (account_id,)).fetchone()
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

    def start_sync(automatic=False):
        with job_lock:
            if job['running']:
                return False
            job.update(running=True, message='Kontakter banken …', error=False, automatic=automatic, history=None)

        def update(message):
            with job_lock:
                job['message'] = message

        def run():
            try:
                result = sync_all(db_path, provider, cipher, update)
                if isinstance(result, dict):
                    message = result.get('message', '')
                    history = result.get('history')
                else:
                    message, history = result, None
                with job_lock:
                    job.update(message=message, history=history, error=False)
            except BankError as error:
                with job_lock:
                    job.update(message=str(error), error=True)
            except Exception as error:
                app.logger.error('Sync failed: %s', type(error).__name__)
                with job_lock:
                    job.update(message='Synkroniseringen fejlede. Eksisterende posteringer er bevaret.', error=True)
            finally:
                with job_lock:
                    job.update(running=False, completed_at=datetime.now(timezone.utc).isoformat())

        threading.Thread(target=run, daemon=True, name='fjordbudget-bank-sync').start()
        return True

    def auto_sync_due():
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=auto_sync_interval)
        with connect(db_path) as db:
            rows = db.execute("""SELECT a.synced_at FROM accounts a
                                 JOIN connections c ON c.id=a.connection_id
                                 WHERE a.source='live' AND a.included=1 AND a.selection_pending=0""").fetchall()
        if not rows:
            return False
        for row in rows:
            if not row['synced_at']:
                return True
            try:
                synced_at = datetime.fromisoformat(row['synced_at'].replace('Z', '+00:00'))
                if synced_at.tzinfo is None:
                    synced_at = synced_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                return True
            if synced_at <= cutoff:
                return True
        return False

    def auto_sync_loop():
        last_attempt = time.monotonic() - auto_sync_interval
        check_every = min(60, auto_sync_interval)
        while True:
            if time.monotonic() - last_attempt >= auto_sync_interval:
                try:
                    if auto_sync_due() and start_sync(automatic=True):
                        last_attempt = time.monotonic()
                except Exception as error:
                    app.logger.error('Automatic sync check failed: %s', type(error).__name__)
                    last_attempt = time.monotonic()
            time.sleep(check_every)

    @app.get('/api/sync')
    def sync_status():
        with job_lock:
            return jsonify(dict(job))

    @app.post('/api/sync')
    def sync():
        if not start_sync():
            return jsonify(error='En synkronisering kører allerede.'), 409
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

    if not app.testing and auto_sync_interval > 0:
        threading.Thread(target=auto_sync_loop, daemon=True, name='fjordbudget-auto-sync').start()

    return app
