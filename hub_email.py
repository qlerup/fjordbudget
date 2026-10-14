"""Mandatory email confirmation for an already authenticated Hub app session."""
import secrets
from urllib.parse import urlsplit
from flask import Response, current_app, jsonify, request, session


def csrf_token():
    return session.get('csrf') or session.setdefault('hub_email_csrf', secrets.token_urlsafe(32))


def required_response():
    if request.method == 'GET' and not request.path.startswith('/api/'):
        response = Response('''<!doctype html><html lang="da"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Opdater email</title>
<link rel="stylesheet" href="/static/hub-session.css?v=email-update1">
<script defer src="/static/hub-session.js?v=email-update1"></script></head>
<body><main><h1>Opdater din email</h1><p>Din administrator kræver, at du bekræfter eller opdaterer din email for at fortsætte.</p></main></body></html>''', content_type='text/html; charset=utf-8')
        response.headers['Cache-Control'] = 'private, no-store'
        return response
    response = jsonify(ok=False, authenticated=True, email_required=True,
                       error_code='email_update_required', csrf_token=csrf_token(),
                       error='Bekræft eller opdater din email for at fortsætte.')
    response.status_code = 403
    response.headers['Cache-Control'] = 'private, no-store'
    return response


def submit_email(member, call):
    expected = csrf_token()
    supplied = request.headers.get('X-CSRF-Token', '')
    if not secrets.compare_digest(expected, supplied):
        return jsonify(ok=False, error='Genindlæs siden og prøv igen.'), 403
    origin = request.headers.get('Origin')
    parsed = urlsplit(origin) if origin else None
    if (parsed and (parsed.scheme not in ('http', 'https') or parsed.netloc.lower() != request.host.lower())) or request.headers.get('Sec-Fetch-Site') == 'cross-site':
        return jsonify(ok=False, error='Ugyldig oprindelse.'), 403
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict) or not isinstance(data.get('email'), str) or len(data['email']) > 254:
        return jsonify(ok=False, error='Indtast en gyldig email.'), 400
    try:
        result = call(f"/api/hub/apps/users/{member['id']}/email", {'email':data['email']}, method='POST')
    except Exception:
        current_app.logger.exception('Could not complete required Hub email update')
        return jsonify(ok=False, error='FjordHub kunne ikke kontaktes. Prøv igen om lidt.'), 503
    return jsonify(result)
