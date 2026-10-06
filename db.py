import calendar
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

CATEGORIES = ['Mad & indkøb', 'Bolig', 'Transport', 'Shopping', 'Fritid', 'Abonnementer', 'Sundhed', 'Andet', 'Indkomst', 'Overførsler']
COLORS = ['#4b8266', '#7184b9', '#d9a453', '#b08ba4', '#74a7a2', '#a58b61', '#b97472', '#8e9594', '#377d65', '#929eae']
CURRENCIES = {'DKK', 'EUR', 'USD', 'GBP', 'SEK', 'NOK', 'CHF', 'PLN'}


def cents(value):
    try:
        amount = Decimal(str(value)) * 100
        if not amount.is_finite() or amount != amount.to_integral_value() or abs(amount) > 10**14:
            raise ValueError('Beløbet skal have højst to decimaler.')
        return int(amount)
    except (InvalidOperation, TypeError):
        raise ValueError('Ugyldigt beløb.') from None


def transaction_title(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).casefold().split())


def transaction_rule_title(value):
    """Stable bank-text key that ignores changing standalone number blocks safely."""
    normalized = transaction_title(value)
    tokens = normalized.split()
    text_tokens = [token for token in tokens if not token.isdigit()]
    alpha_signal = sum(sum(char.isalpha() for char in token) for token in text_tokens)
    # Do not generalize very short labels such as "MCD 06151"; they are too ambiguous.
    if len(text_tokens) < 2 or alpha_signal < 6:
        return normalized
    return ' '.join(text_tokens)


@contextmanager
def connect(path):
    conn = sqlite3.connect(path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.create_function('transaction_title', 1, transaction_title, deterministic=True)
    conn.create_function('transaction_rule_title', 1, transaction_rule_title, deterministic=True)
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize(path):
    with connect(path) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript('''
        CREATE TABLE IF NOT EXISTS connections (
          id TEXT PRIMARY KEY, bank TEXT NOT NULL, session_token TEXT NOT NULL,
          valid_until TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS accounts (
          id TEXT PRIMARY KEY, source TEXT NOT NULL CHECK(source IN ('demo','live')),
          connection_id TEXT REFERENCES connections(id), remote_id TEXT,
          name TEXT NOT NULL, bank TEXT NOT NULL, last4 TEXT NOT NULL,
          currency TEXT NOT NULL, balance INTEGER, balance_type TEXT, synced_at TEXT,
          included INTEGER NOT NULL DEFAULT 1 CHECK(included IN (0,1)),
          selection_pending INTEGER NOT NULL DEFAULT 0 CHECK(selection_pending IN (0,1)));
        CREATE TABLE IF NOT EXISTS transactions (
          id INTEGER PRIMARY KEY, account_id TEXT NOT NULL REFERENCES accounts(id),
          external_id TEXT NOT NULL, booked_on TEXT NOT NULL, description TEXT NOT NULL,
          amount INTEGER NOT NULL, currency TEXT NOT NULL, category TEXT NOT NULL,
          category_manual INTEGER NOT NULL DEFAULT 0,
          merchant TEXT, merchant_manual INTEGER NOT NULL DEFAULT 0,
          UNIQUE(account_id,external_id));
        CREATE INDEX IF NOT EXISTS tx_account_date ON transactions(account_id,booked_on DESC);
        CREATE TABLE IF NOT EXISTS category_rules (
          source TEXT NOT NULL CHECK(source IN ('demo','live')),
          title TEXT NOT NULL, category TEXT NOT NULL,
          PRIMARY KEY(source,title));
        CREATE TABLE IF NOT EXISTS merchant_rules (
          source TEXT NOT NULL CHECK(source IN ('demo','live')),
          title TEXT NOT NULL, merchant TEXT NOT NULL,
          PRIMARY KEY(source,title));
        CREATE TABLE IF NOT EXISTS categories (
          name TEXT PRIMARY KEY, color TEXT NOT NULL, protected INTEGER NOT NULL DEFAULT 0,
          requires_merchant INTEGER NOT NULL DEFAULT 1 CHECK(requires_merchant IN (0,1)));
        CREATE TABLE IF NOT EXISTS budget_categories (name TEXT PRIMARY KEY, color TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS savings_goals (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          source TEXT NOT NULL CHECK(source IN ('demo','live')),
          currency TEXT NOT NULL, name TEXT NOT NULL,
          target_amount INTEGER NOT NULL CHECK(target_amount>0),
          saved_amount INTEGER NOT NULL DEFAULT 0 CHECK(saved_amount>=0),
          deadline TEXT NOT NULL,
          featured INTEGER NOT NULL DEFAULT 0 CHECK(featured IN (0,1)));
        CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS budgets (
          source TEXT NOT NULL, month TEXT NOT NULL, currency TEXT NOT NULL,
          category TEXT NOT NULL, amount INTEGER NOT NULL CHECK(amount>=0),
          PRIMARY KEY(source,month,currency,category));
        CREATE TABLE IF NOT EXISTS oauth_states (
          state_hash TEXT PRIMARY KEY, browser_hash TEXT NOT NULL, bank TEXT NOT NULL,
          expires REAL NOT NULL);
        PRAGMA user_version=1;
        ''')
        db.execute('BEGIN IMMEDIATE')
        if not db.execute("SELECT 1 FROM app_migrations WHERE name='categories'").fetchone():
            db.executemany('INSERT OR IGNORE INTO categories(name,color,protected) VALUES (?,?,?)',
                [(name,COLORS[i],int(name in ('Andet','Indkomst','Overførsler'))) for i,name in enumerate(CATEGORIES)])
            db.execute("INSERT INTO app_migrations VALUES ('categories')")
        category_columns = {row[1] for row in db.execute('PRAGMA table_info(categories)')}
        if 'requires_merchant' not in category_columns:
            db.execute('ALTER TABLE categories ADD COLUMN requires_merchant INTEGER NOT NULL DEFAULT 1')
        if not db.execute("SELECT 1 FROM app_migrations WHERE name='category-merchant-requirement'").fetchone():
            db.execute("UPDATE categories SET requires_merchant=0 WHERE name IN ('Indkomst','Overførsler')")
            db.execute("INSERT INTO app_migrations VALUES ('category-merchant-requirement')")
        account_columns = {row[1] for row in db.execute('PRAGMA table_info(accounts)')}
        if 'custom_name' not in account_columns:
            db.execute('ALTER TABLE accounts ADD COLUMN custom_name TEXT')
        if 'included' not in account_columns:
            db.execute('ALTER TABLE accounts ADD COLUMN included INTEGER NOT NULL DEFAULT 1')
        if 'selection_pending' not in account_columns:
            db.execute('ALTER TABLE accounts ADD COLUMN selection_pending INTEGER NOT NULL DEFAULT 0')
        transaction_columns = {row[1] for row in db.execute('PRAGMA table_info(transactions)')}
        if 'merchant' not in transaction_columns:
            db.execute('ALTER TABLE transactions ADD COLUMN merchant TEXT')
        if 'merchant_manual' not in transaction_columns:
            db.execute('ALTER TABLE transactions ADD COLUMN merchant_manual INTEGER NOT NULL DEFAULT 0')
        goal_columns = {row[1] for row in db.execute('PRAGMA table_info(savings_goals)')}
        if 'saved_amount' not in goal_columns:
            db.execute('ALTER TABLE savings_goals ADD COLUMN saved_amount INTEGER NOT NULL DEFAULT 0')
        if 'featured' not in goal_columns:
            db.execute('ALTER TABLE savings_goals ADD COLUMN featured INTEGER NOT NULL DEFAULT 0')
        db.execute('CREATE INDEX IF NOT EXISTS tx_merchant ON transactions(merchant)')
        seed_demo(db)
        if not db.execute("SELECT 1 FROM app_migrations WHERE name='demo-merchants'").fetchone():
            db.execute("""UPDATE transactions SET merchant=description
                          WHERE account_id IN (SELECT id FROM accounts WHERE source='demo')
                            AND amount<0 AND category!='Overførsler' AND merchant IS NULL""")
            db.execute("INSERT INTO app_migrations VALUES ('demo-merchants')")
        if not db.execute("SELECT 1 FROM app_migrations WHERE name='separate-budget-categories'").fetchone():
            db.executemany('INSERT OR IGNORE INTO budget_categories VALUES (?,?)', list(zip(CATEGORIES[:8],COLORS[:8])))
            db.execute("INSERT OR IGNORE INTO budget_categories SELECT DISTINCT category,'#809087' FROM budgets")
            db.execute('ALTER TABLE categories ADD COLUMN budget_category TEXT REFERENCES budget_categories(name)')
            db.execute('UPDATE categories SET budget_category=name WHERE name IN (SELECT name FROM budget_categories)')
            db.execute("INSERT INTO app_migrations VALUES ('separate-budget-categories')")


def category_list(db):
    return [dict(row) for row in db.execute('SELECT name,color,protected,budget_category,requires_merchant FROM categories ORDER BY rowid')]


def budget_category_list(db):
    return [dict(row) for row in db.execute('SELECT name,color FROM budget_categories ORDER BY rowid')]


def seed_demo(db):
    if db.execute("SELECT 1 FROM accounts WHERE source='demo'").fetchone():
        return
    today = date.today()
    for aid, name, tail, balance in [('daily', 'Lønkonto', '4821', 2487650), ('bills', 'Budgetkonto', '9036', 1260000), ('save', 'Opsparing', '7150', 6840000)]:
        db.execute('INSERT INTO accounts (id,source,name,bank,last4,currency,balance,balance_type,synced_at) VALUES (?,?,?,?,?,?,?,?,?)',
                   ('demo-'+aid, 'demo', name, 'Eksempelbanken', tail, 'DKK', balance, 'CLBD', today.isoformat()))
    shops = [('REMA 1000', 'Mad & indkøb', -38650), ('Netto', 'Mad & indkøb', -21495), ('DSB', 'Transport', -9600),
             ('Bageriet på hjørnet', 'Mad & indkøb', -6800), ('Café Central', 'Fritid', -18500), ('Matas', 'Sundhed', -14995),
             ('Føtex', 'Mad & indkøb', -57320), ('ARKET', 'Shopping', -49900), ('Q8', 'Transport', -46250)]
    for months_back in range(4):
        ordinal = today.year * 12 + today.month - 1 - months_back
        year, m0 = divmod(ordinal, 12)
        first = date(year, m0+1, 1)
        last = min(calendar.monthrange(year, m0+1)[1], today.day) if months_back == 0 else calendar.monthrange(year, m0+1)[1]
        rows = [(1, 'daily', 'Løn', 3250000, 'Indkomst'), (1, 'bills', 'Husleje', -820000, 'Bolig'),
                (2, 'bills', 'Internet · Waoo', -29900, 'Abonnementer'), (3, 'daily', 'Spotify', -10900, 'Abonnementer'),
                (1, 'daily', 'Til budgetkonto', -1100000, 'Overførsler'), (1, 'bills', 'Fra lønkonto', 1100000, 'Overførsler'),
                (2, 'daily', 'Til opsparing', -350000, 'Overførsler'), (2, 'save', 'Fra lønkonto', 350000, 'Overførsler')]
        for day in range(2, last+1):
            label, cat, amount = shops[(day+months_back*2) % len(shops)]
            rows.append((day, 'daily', label, amount - (day % 3)*100, cat))
        for i, (day, aid, label, amount, cat) in enumerate(rows):
            if day > last:
                continue
            booked = first.replace(day=day).isoformat()
            db.execute('INSERT INTO transactions(account_id,external_id,booked_on,description,amount,currency,category,merchant) VALUES (?,?,?,?,?,?,?,?)',
                       ('demo-'+aid, f'{first}-{i}', booked, label, amount, 'DKK', cat,
                        label if amount < 0 and cat != 'Overførsler' else None))
        for cat, amount in zip(CATEGORIES[:8], [450000, 850000, 180000, 150000, 180000, 80000, 70000, 120000]):
            db.execute('INSERT INTO budgets VALUES (?,?,?,?,?)', ('demo', first.strftime('%Y-%m'), 'DKK', cat, amount))


def categorize(description, amount):
    label = description.casefold()
    for terms, cat in [(['overførsel', 'opsparing'], 'Overførsler'), (['netto', 're ma', 'rema', 'føtex', 'lidl', 'superbrugsen', 'bager'], 'Mad & indkøb'),
                       (['husleje', 'realkredit'], 'Bolig'), (['dsb', 'rejsekort', 'q8', 'circle k'], 'Transport'),
                       (['netflix', 'spotify', 'waoo', 'yousee'], 'Abonnementer')]:
        if any(term in label for term in terms):
            return cat
    return 'Indkomst' if amount > 0 else 'Andet'
