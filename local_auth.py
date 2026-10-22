"""The same first-admin setup as FjordLens when no FjordHub manages the app."""
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit

from flask import g,jsonify,redirect,render_template,request,session
from werkzeug.security import check_password_hash,generate_password_hash
from db import connect


def register_local_auth(app,db_path):
    app.config['TRUSTED_HOSTS']=None  # Claimed hosts are checked below after authentication.
    marker=Path(app.config['DATA_DIR'])/'local-install-initialized'
    proxies={value.strip() for value in app.config['TRUSTED_PROXY_IPS'].split(',') if value.strip()}
    with connect(db_path) as db:
        db.executescript('''CREATE TABLE IF NOT EXISTS local_owner (
            id INTEGER PRIMARY KEY CHECK(id=1),username TEXT NOT NULL,
            password_hash TEXT NOT NULL,auth_version TEXT NOT NULL,public_origin TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS local_login_attempts (
                bucket TEXT PRIMARY KEY,window INTEGER NOT NULL,count INTEGER NOT NULL);''')

    def owner():
        with connect(db_path) as db:
            row=db.execute('SELECT * FROM local_owner WHERE id=1').fetchone()
        if row and not marker.exists():
            marker.touch(mode=0o600,exist_ok=True)
        return row

    def origin():
        parsed=urlsplit(request.host_url)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Ugyldig app-adresse.')
        scheme=request.scheme
        # Only the configured local proxy can assert HTTPS. Forwarded Host is
        # deliberately ignored; the real Host header must be preserved.
        if request.remote_addr in proxies:
            forwarded=request.headers.get('X-Forwarded-Proto','').split(',')[0].strip()
            if forwarded in ('http','https'):
                scheme=forwarded
        configured=urlsplit(app.config['APP_URL'])
        if configured.scheme=='https' and configured.netloc.lower()==request.host.lower():
            scheme='https'
        return scheme+'://'+request.host

    def authenticated(row):
        return bool(row and session.get('local_auth_version')==row['auth_version']
            and 0<=time.time()-session.get('login_at',0)<43200)

    def limit(kind):
        address=request.remote_addr or 'unknown'
        bucket=hashlib.sha256((kind+address).encode()).hexdigest()
        window=int(time.time())//300
        with connect(db_path) as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('DELETE FROM local_login_attempts WHERE window<?',(window-2,))
            db.execute('''INSERT INTO local_login_attempts VALUES (?,?,1) ON CONFLICT(bucket)
                DO UPDATE SET count=CASE WHEN window=excluded.window THEN count+1 ELSE 1 END,
                window=excluded.window''',(bucket,window))
            return db.execute('SELECT count FROM local_login_attempts WHERE bucket=?',(bucket,)).fetchone()[0]<=10

    def form_error():
        expected=session.get('csrf','')
        if not expected or not secrets.compare_digest(expected,request.form.get('csrf','')):
            return 'Sessionen er udløbet. Genindlæs siden.',403
        if request.headers.get('Origin') not in (None,g.budget_origin):
            return 'Forespørgslen skal komme fra denne app.',403
        return None

    def render(setup=False,error=None,code=200,created=False):
        session.setdefault('csrf',secrets.token_urlsafe(32))
        return render_template('local_login.html',setup=setup,error=error,created=created,
            installation_name=app.config['INSTALLATION_NAME']),code

    @app.before_request
    def local_login_required():
        g.budget_origin=origin()
        request.environ['budget_https']=g.budget_origin.startswith('https://')
        if request.endpoint in ('health','static','privacy','terms'):
            return
        row=owner()
        if not row and marker.exists():
            return jsonify(error='Installationen har været opsat, men ejerdata mangler. Gendan ejerens data fra backup.'),503
        if request.endpoint in ('setup','login','api_local_access'):
            return
        if not row:
            if request.path.startswith('/api/'):
                return jsonify(error='Opret installationens ejer først.',setup_required=True),401
            return redirect('/setup')
        if authenticated(row):
            allowed={urlsplit(app.config['APP_URL']).netloc.lower(),urlsplit(row['public_origin']).netloc.lower()}
            if request.host.lower() not in allowed:
                return jsonify(error='Log ind på denne adresse for at bekræfte den som app-adresse.'),400
            return
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify(error='Log ind for at fortsætte.'),401
        return redirect('/login')

    @app.route('/setup',methods=['GET','POST'])
    def setup():
        if owner():
            return redirect('/login')
        if request.method=='POST':
            problem=form_error()
            if problem:
                return render(True,*problem)
            if not limit('setup'):
                return render(True,'For mange forsøg. Vent fem minutter.',429)
            username=request.form.get('username','').strip()
            password=request.form.get('password','')
            if not 2<=len(username)<=60 or any(ord(c)<32 for c in username):
                return render(True,'Vælg et brugernavn på 2–60 tegn.',400)
            if not 8<=len(password)<=128:
                return render(True,'Adgangskoden skal være på 8–128 tegn.',400)
            if password!=request.form.get('password2',''):
                return render(True,'Adgangskoderne er ikke ens.',400)
            password_hash=generate_password_hash(password)
            with connect(db_path) as db:
                db.execute('BEGIN IMMEDIATE')
                if db.execute('SELECT 1 FROM local_owner').fetchone():
                    return redirect('/login')
                if marker.exists():
                    return render(True,'Installationen er allerede opsat. Gendan ejerdata.',409)
                db.execute('INSERT INTO local_owner VALUES (1,?,?,?,?)',
                    (username,password_hash,secrets.token_urlsafe(32),g.budget_origin))
            marker.touch(mode=0o600,exist_ok=True)
            session.clear()
            return redirect('/login?created=1')
        return render(True)

    @app.route('/login',methods=['GET','POST'])
    def login():
        row=owner()
        if not row:
            return redirect('/setup')
        if request.method=='POST':
            problem=form_error()
            if problem:
                return render(False,*problem)
            if not limit('login'):
                return render(False,'For mange forsøg. Vent fem minutter.',429)
            username=request.form.get('username','')[:200]
            password=request.form.get('password','')[:1024]
            valid_password=check_password_hash(row['password_hash'],password)
            if username==row['username'] and valid_password:
                # A successful owner login confirms a newly assigned domain;
                # no unauthenticated request can change the stored app origin.
                with connect(db_path) as db:
                    db.execute('UPDATE local_owner SET public_origin=? WHERE id=1',(g.budget_origin,))
                session.clear()
                session.update(local_auth_version=row['auth_version'],user_name=row['username'],
                    login_at=time.time(),csrf=secrets.token_urlsafe(32),browser=secrets.token_urlsafe(32))
                session.permanent=True
                return redirect('/')
            return render(False,'Brugernavn eller adgangskode er forkert.',401)
        return render(created=request.args.get('created')=='1')

    @app.post('/logout')
    def logout():
        session.clear()
        return jsonify(ok=True)

    @app.get('/api/auth/access')
    def api_local_access():
        return jsonify(authenticated=authenticated(owner()))

    return lambda:g.budget_origin
