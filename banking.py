"""Read-only Enable Banking AIS integration. No payment endpoints."""
import hashlib
import json
import re
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import jwt
import requests

from db import CURRENCIES, categorize, cents, connect, transaction_title


class BankError(Exception):
    pass


def account_name(account):
    # Enable Banking's `name` is the holder, not the account label.
    for field in ('details', 'product'):
        value = account.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:200]
    return 'Bankkonto'


class EnableBanking:
    def __init__(self, app_id, key_file, credential_store=None, cipher=None):
        self.app_id, self.key_file = app_id, key_file
        self.credential_store, self.cipher = credential_store, cipher

    def credentials(self):
        if self.credential_store and self.credential_store.exists():
            saved = json.loads(self.cipher.decrypt(self.credential_store.read_bytes()))
            return saved['app_id'], saved['private_key'].encode()
        return self.app_id, Path(self.key_file).read_bytes()

    @property
    def configured(self):
        return bool((self.credential_store and self.credential_store.is_file()) or
                    (self.app_id and Path(self.key_file).is_file()))

    def request(self, method, path, **kwargs):
        # Keep the transport itself restricted to account-information services.
        allowed = ((method == 'GET' and re.fullmatch(r'/(aspsps|application|accounts/[^/]+/(balances|transactions|details))', path))
                   or (method == 'POST' and path in ('/auth', '/sessions'))
                   or (method == 'DELETE' and re.fullmatch(r'/sessions/[^/]+', path)))
        if not allowed:
            raise BankError('FjordBudget tillader kun læseadgang og administration af bankens læsesamtykke.')
        if not self.configured:
            raise BankError('Bankforbindelsen mangler opsætning. Se guiden under Forbind bank.')
        try:
            now = int(time.time())
            app_id, private_key = self.credentials()
            token = jwt.encode({'iss': 'enablebanking.com', 'aud': 'api.enablebanking.com', 'iat': now, 'exp': now+300},
                               private_key, algorithm='RS256', headers={'kid': app_id})
        except (ValueError, OSError, jwt.PyJWTError):
            raise BankError('API-nøglen kunne ikke læses. Kontroller app-id og privat nøgle.') from None
        try:
            response = requests.request(method, 'https://api.enablebanking.com'+path,
                                        headers={'Authorization': 'Bearer '+token, 'Accept': 'application/json'},
                                        timeout=(5, 35), **kwargs)
            if response.status_code in (401, 403):
                raise BankError('Adgang afvist. Kontroller API-opsætningen og bankens samtykke.')
            if response.status_code == 429:
                raise BankError('Bankens forespørgselsgrænse er nået. Prøv igen senere.')
            if not response.ok:
                raise BankError(f'Banktjenesten svarede med fejl {response.status_code}. Prøv igen senere.')
            return response.json()
        except (requests.RequestException, ValueError):
            raise BankError('Banktjenesten kunne ikke nås eller gav et ugyldigt svar. Prøv igen senere.') from None

    def banks(self):
        return self.request('GET', '/aspsps', params={'country': 'DK'}).get('aspsps', [])

    def transactions(self, uid, start, end):
        params = {'date_from': start, 'date_to': end, 'transaction_status': 'BOOK'}
        items, seen = [], set()
        for _ in range(200):
            page = self.request('GET', f'/accounts/{quote(uid, safe="")}/transactions', params=dict(params))
            items.extend(page.get('transactions', []))
            key = page.get('continuation_key')
            if not key:
                return items
            if key in seen:
                raise BankError('Banken gentog en side. Ingen delvis import er gemt for kontoen.')
            seen.add(key)
            params['continuation_key'] = key
        raise BankError('For mange sider fra banken. Ingen delvis import er gemt for kontoen.')


def normalize_transactions(items):
    counts, result = Counter(), []
    for item in items:
        if item.get('status') != 'BOOK':
            continue
        currency = item['transaction_amount']['currency']
        if currency not in CURRENCIES:
            raise BankError(f'Valutaen {currency} understøttes endnu ikke. Kontoen blev ikke importeret.')
        amount = abs(cents(item['transaction_amount']['amount']))
        direction = item.get('credit_debit_indicator')
        if direction not in ('DBIT', 'CRDT'):
            raise BankError('En postering mangler gyldig ind-/udbetalingsretning.')
        if direction == 'DBIT':
            amount = -amount
        booked = item.get('booking_date') or item.get('value_date') or item.get('transaction_date')
        if not booked:
            raise BankError('En bogført postering mangler dato. Kontoen blev ikke importeret.')
        booked = date.fromisoformat(booked).isoformat()
        remittance = ' · '.join(item.get('remittance_information') or [])
        party = item.get('creditor' if amount < 0 else 'debtor') or {}
        description = (remittance or party.get('name') or 'Postering')[:1000]
        external = item.get('entry_reference')
        if external:
            external = 'ref:'+str(external)
        else:
            # transaction_id is not stable between fetches. Preserve identical
            # same-day purchases by giving each fingerprint an occurrence index.
            fingerprint = hashlib.sha256(json.dumps([booked, amount, currency, description], ensure_ascii=False).encode()).hexdigest()
            counts[fingerprint] += 1
            external = f'fallback:{fingerprint}:{counts[fingerprint]}'
        result.append((external, booked, description, amount, currency, categorize(description, amount)))
    return result


def import_account(db_path, account_id, transactions, balances):
    rows = normalize_transactions(transactions)
    # Prefer booked balances; never replace an unavailable balance with zero.
    ranking = {'CLBD': 0, 'ITBD': 1, 'CLAV': 2, 'ITAV': 3, 'OPBD': 4}
    with connect(db_path) as db:
        db.execute('BEGIN IMMEDIATE')
        account = db.execute('SELECT * FROM accounts WHERE id=?', (account_id,)).fetchone()
        rules = dict(db.execute('SELECT title,category FROM category_rules WHERE source=?', (account['source'],)).fetchall())
        eligible = [b for b in balances if b.get('balance_type') in ranking and b.get('balance_amount', {}).get('currency') == account['currency']]
        balance = min(eligible, key=lambda b: ranking[b['balance_type']]) if eligible else None
        for row in rows:
            rule = rules.get(transaction_title(row[2]))
            row = (*row[:-1], rule or row[-1], int(rule is not None))
            db.execute('''INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category,category_manual)
              VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(account_id,external_id) DO UPDATE SET
              booked_on=excluded.booked_on, description=excluded.description, amount=excluded.amount,
              currency=excluded.currency, category=CASE WHEN excluded.category_manual=1 THEN excluded.category WHEN transactions.category_manual=1 THEN transactions.category ELSE excluded.category END,
              category_manual=MAX(transactions.category_manual,excluded.category_manual)''',
                       (account_id, *row))
        db.execute('UPDATE accounts SET balance=?,balance_type=?,synced_at=? WHERE id=?',
                   (cents(balance['balance_amount']['amount']) if balance else None,
                    balance['balance_type'] if balance else None, datetime.now(timezone.utc).isoformat(), account_id))
    return len(rows)


def sync_all(db_path, provider, cipher, progress):
    with connect(db_path) as db:
        accounts = [dict(r) for r in db.execute("SELECT a.*,c.valid_until FROM accounts a JOIN connections c ON c.id=a.connection_id WHERE a.source='live'")]
    if not accounts:
        raise BankError('Forbind en bank, før du synkroniserer.')
    errors = []
    for i, account in enumerate(accounts):
        progress(f'Henter konto {i+1} af {len(accounts)} …')
        try:
            if datetime.fromisoformat(account['valid_until'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
                raise BankError('Samtykket er udløbet. Forbind banken igen.')
            uid = cipher.decrypt(account['remote_id'].encode()).decode()
            balances = provider.request('GET', f'/accounts/{quote(uid, safe="")}/balances').get('balances', [])
            today = date.today()
            items = provider.transactions(uid, (today-timedelta(days=90)).isoformat(), today.isoformat())
            import_account(db_path, account['id'], items, balances)
            details = provider.request('GET', f'/accounts/{quote(uid, safe="")}/details')
            with connect(db_path) as db:
                db.execute('UPDATE accounts SET name=? WHERE id=?', (account_name(details), account['id']))
        except (BankError, ValueError, KeyError) as error:
            errors.append(f'{account["name"]}: {str(error) if isinstance(error, BankError) else "Uventet dataformat fra banken."}')
    if errors:
        raise BankError(' '.join(errors))
    return f'{len(accounts)} konti opdateret. Seneste 90 dages bogførte posteringer hentet.'
