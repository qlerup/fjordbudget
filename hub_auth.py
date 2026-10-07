"""FjordHub authentication for a single owner's private budget installation."""
import ipaddress
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

import requests
from flask import g, jsonify, redirect, render_template, request, session
from flask.sessions import SecureCookieSessionInterface

from db import connect


class HubUnavailable(Exception):
    pass


class BudgetCookies(SecureCookieSessionInterface):
    def get_cookie_secure(self, app):
        return bool(request.environ.get('budget_https') or super().get_cookie_secure(app))


def register_hub_auth(app, db_path):
    managed = app.config['AUTH_MODE'] == 'fjordhub'
    app.session_interface = BudgetCookies()
    if not managed:
        if app.config['AUTH_MODE'] != 'local':
            raise RuntimeError('Unknown AUTH_MODE')
        return lambda: app.config['APP_URL']
    if not app.config['FJORDHUB_URL'] or not app.config['FJORDHUB_API_KEY']:
        raise RuntimeError('FjordHub mode requires URL and API key')
    app.config['TRUSTED_HOSTS'] = None  # Validated against Hub's configured domain below.
    with connect(db_path) as db:
        db.execute('CREATE TABLE IF NOT EXISTS budget_owner (singleton INTEGER PRIMARY KEY CHECK(singleton=1), hub_id INTEGER NOT NULL)')
    cache, lock = {'at': 0, 'url': ''}, threading.Lock()
    attempts = defaultdict(deque)

    def hub_api(path, payload=None, method='GET'):
        data = {'app_id': 'fjordbudget', **(payload or {})}
        try:
            response = requests.request(method, app.config['FJORDHUB_URL'].rstrip('/')+path,
                headers={'X-Hub-Key': app.config['FJORDHUB_API_KEY']},
                **({'params': data} if method == 'GET' else {'json': data}), timeout=(3, 6))
            if response.status_code >= 500:
                raise HubUnavailable()
            return response.json() if response.ok else {'ok': False}
        except (requests.RequestException, ValueError):
            raise HubUnavailable() from None

    app.extensions['hub_api'] = hub_api

    def public_url():
        with lock:
            if time.monotonic()-cache['at'] > 30:
                result = hub_api('/api/hub/apps/config')
                if not result.get('ok'):
                    raise HubUnavailable()
                value = str(result.get('external_url') or '').rstrip('/')
                parsed = urlsplit(value)
                if value and (parsed.scheme not in ('http', 'https') or not parsed.hostname
                              or parsed.username or parsed.password or parsed.path not in ('', '/')):
                    raise HubUnavailable()
                cache.update(at=time.monotonic(), url=value)
        return cache['url'] or request.host_url.rstrip('/')

    @app.errorhandler(HubUnavailable)
    def unavailable(error):
        if request.path.startswith('/api/'):
            return jsonify(error='FjordHub kunne ikke kontaktes. Prøv igen om lidt.'), 503
        return render_template('login.html', error='FjordHub kunne ikke kontaktes. Prøv igen om lidt.'), 503

    def owner_id():
        with connect(db_path) as db:
            row = db.execute('SELECT hub_id FROM budget_owner WHERE singleton=1').fetchone()
        return row['hub_id'] if row else None

    def establish(user):
        if not isinstance(user, dict) or type(user.get('id')) is not int or user['id'] <= 0 or user.get('must_change_password'):
            return False
        uid = user['id']
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT hub_id FROM budget_owner WHERE singleton=1').fetchone()
            if not row:
                if user.get('role') != 'admin':
                    return False
                db.execute('INSERT INTO budget_owner VALUES(1,?)', (uid,))
            elif row['hub_id'] != uid:
                return False
        session.clear()
        session.update(hub_user_id=uid, user_name=user.get('username',''), csrf=secrets.token_urlsafe(32),
                       browser=secrets.token_urlsafe(32), login_at=time.time())
        session.permanent = True
        return True

    @app.before_request
    def require_owner():
        # Trust the domain stored by Hub, not forwarded host/proto headers.
        host = request.host.split(':')[0].lower()
        try:
            local_host = ipaddress.ip_address(host).is_private
        except ValueError:
            local_host = host in ('localhost', 'host.docker.internal')
        if local_host:
            origin = request.host_url.rstrip('/')
        else:
            origin = public_url()
            if request.host.lower() != urlsplit(origin).netloc.lower():
                return jsonify(error='Ukendt app-adresse.'), 400
        request.environ['budget_https'] = urlsplit(origin).scheme == 'https'
        g.budget_origin = origin
        # Health and static policy assets do not require contacting Hub on LAN.
        if request.endpoint in ('privacy','terms','health','static','login','hub_login'):
            return None
        uid = session.get('hub_user_id')
        if uid and time.time()-session.get('login_at',0) < 43200 and uid == owner_id():
            result = hub_api('/api/hub/apps/users')
            if not result.get('ok'):
                raise HubUnavailable()
            user = next((u for u in result.get('items',[]) if u.get('id')==uid and not u.get('must_change_password')), None)
            if user:
                return None
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify(error='Log ind via FjordHub for at fortsætte.'), 401
        return redirect('/login')

    @app.route('/login', methods=['GET','POST'])
    def login():
        session.setdefault('csrf', secrets.token_urlsafe(32))
        error = None
        if request.method == 'POST':
            if not secrets.compare_digest(session['csrf'], request.form.get('csrf','')):
                return render_template('login.html', error='Genindlæs siden og prøv igen.'), 403
            if request.headers.get('Origin') and request.headers['Origin'] != g.budget_origin:
                return render_template('login.html', error='Ugyldig forespørgsel.'), 403
            rate_key = request.remote_addr or 'unknown'
            with lock:
                now = time.monotonic()
                for key in list(attempts):
                    while attempts[key] and attempts[key][0] < now-300:
                        attempts[key].popleft()
                    if not attempts[key]:
                        del attempts[key]
                bucket = attempts[rate_key]
                if len(bucket) >= 5 or len(attempts) > 2048:
                    return render_template('login.html', error='For mange mislykkede forsøg. Vent fem minutter.'), 429
            result = hub_api('/api/hub/apps/authenticate', {'username':request.form.get('username','')[:200],
                'password':request.form.get('password','')[:1024]}, method='POST')
            if result.get('ok') and establish(result.get('user')):
                with lock:
                    attempts.pop(rate_key, None)
                return redirect('/')
            with lock:
                attempts[rate_key].append(time.monotonic())
            error = 'Login afvist. Brug ejeren af dette budget med adgang i FjordHub. En påkrævet kodeændring skal gennemføres i FjordHub først.'
        return render_template('login.html', error=error)

    @app.get('/hub-login')
    def hub_login():
        token = request.args.get('token','')
        if not token or len(token)>512:
            return redirect('/login')
        result = hub_api('/api/hub/sso-verify', {'token':token})
        if result.get('ok') and establish(result):
            return redirect('/')
        return redirect('/login')

    @app.post('/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    return public_url
