"""Conservative merchant recognition from remembered rules and manual corrections."""
import re
import unicodedata
from collections import defaultdict

from db import transaction_title, transaction_rule_title

_CARD_PREFIXES = {'mcd', 'pos', 'kortkoeb', 'kortkøb', 'kortbetaling',
                  'dankort', 'visa', 'mastercard', 'debit'}
_TRANSFER_PREFIXES = {'mobilepay', 'straksoverførsel', 'straksoverfoersel'}
_GENERIC_ANCHORS = _CARD_PREFIXES | _TRANSFER_PREFIXES | {
    'betaling', 'betalt', 'overførsel', 'overfoersel', 'transfer',
    'bank', 'butik', 'shop', 'store', 'online', 'restaurant',
    'cafe', 'køb', 'koeb', 'terminal', 'ref', 'nummer', 'fra', 'til',
}


def _tokens(value):
    value = unicodedata.normalize('NFKC', str(value or '')).casefold()
    return re.findall(r'[^\W_]+', value, re.UNICODE)


def _signature(value):
    tokens = _tokens(value)
    if not tokens:
        return None
    transfer = tokens[0] in _TRANSFER_PREFIXES
    if transfer:
        tokens = tokens[1:]
    elif tokens[0] in _CARD_PREFIXES:
        tokens = tokens[1:]
        # Bank terminal/card reference: not part of a store name.
        if tokens and tokens[0].isdigit():
            tokens = tokens[1:]
    if not tokens or not any(any(c.isalpha() for c in token) for token in tokens):
        return None
    return ('transfer' if transfer else 'purchase', tuple(tokens))


def _unique(mapping):
    return {key: next(iter(values)) for key, values in mapping.items() if len(values) == 1}


def _brand_matches(tokens, merchant, bank_text):
    """Recognize a merchant name as a separate brand, never as a substring.

    Short/mutable bank aliases only work for the matching confirmed merchant.
    """
    if not tokens:
        return False
    name = _tokens(merchant)
    if not name:
        return False
    normalized_name = ''.join(name)
    first = tokens[0]
    if normalized_name in {'mcdonalds', 'mcdonald'}:
        # MCD is a generic bank-card prefix, NOT proof of McDonald's.
        # Require a subsequent store token like MCDMARIBO after a terminal ID.
        bank_tokens = _tokens(bank_text)
        card_context = (len(bank_tokens) >= 3 and bank_tokens[0] == 'mcd'
                        and bank_tokens[1].isdigit())
        return (first in {'mcdonalds', 'mcdonald'}
                or (card_context and first.startswith('mcd')
                    and len(first) >= 6 and first.isalpha()))
    if normalized_name in {'hm', 'hennesmauritz'}:
        # 'HM' alone is ambiguous; require a store code such as DK0829.
        return first == 'hm' and len(tokens) > 1 and bool(re.fullmatch(r'dk\d{3,}', tokens[1]))
    if len(first) < 4 or not first.isalpha() or first in _GENERIC_ANCHORS:
        return False
    if first != name[0]:
        return False
    if len(name) == 1:
        return True
    # Names with purely numeric, web-domain or corporate suffixes identify
    # the main brand without requiring the same store/terminal number.
    if name[1].isdigit() or name[1] in {'dk', 'com', 'eu'}:
        return True
    meaningful = name[:]
    while meaningful and meaningful[-1] in {'aps', 'as', 'a', 's'}:
        meaningful.pop()
    return len(tokens) >= len(meaningful) and list(tokens[:len(meaningful)]) == meaningful


class MerchantMatcher:
    """Default MobilePay > remembered overrides > confirmed distinct brands.

    Broad brand matching uses manual confirmations/remembered rules only.
    Ambiguous and conflicting bank text is never generalized automatically.
    """

    def __init__(self, rule_rows, manual_rows=()):
        exact, stable, equivalent = defaultdict(set), defaultdict(set), defaultdict(set)
        anchors = defaultdict(lambda: defaultdict(lambda: {'rules': set(), 'manual_count': 0}))
        anchor_owners = defaultdict(set)
        names = {}

        def remember_name(merchant):
            key = ''.join(_tokens(merchant))
            if key:
                names.setdefault(key, merchant)
            return key

        def record_anchor(description, merchant, *, remembered=False, count=0):
            signature = _signature(description)
            if signature is None:
                return
            kind, tokens = signature
            if kind != 'purchase':
                return
            anchor = tokens[0]
            owner = remember_name(merchant)
            if len(anchor) < 4 or not anchor.isalpha() or anchor in _GENERIC_ANCHORS:
                return
            anchor_owners[anchor].add(owner)
            if not _brand_matches(tokens, merchant, description):
                return
            example = anchors[anchor][owner]
            example.setdefault('merchant', merchant)
            if remembered:
                example['rules'].add(signature)
            if count:
                example['manual_count'] += count

        for row in rule_rows:
            title, merchant = row['title'], row['merchant']
            exact[transaction_title(title)].add(merchant)
            stable[transaction_rule_title(title)].add(merchant)
            remember_name(merchant)
            signature = _signature(title)
            if not signature:
                continue
            kind, tokens = signature
            if len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 5 and tokens[0].isalpha()):
                equivalent[signature].add(merchant)
            record_anchor(title, merchant, remembered=True)

        for row in manual_rows:
            merchant = (row['merchant'] or '').strip()
            if not merchant:
                continue
            saved = stable.get(transaction_rule_title(row['description']), set())
            # Do not turn one-off exceptions to a remembered rule into a
            # contradictory brand-wide inference.
            if saved and ''.join(_tokens(merchant)) not in {
                    ''.join(_tokens(name)) for name in saved}:
                continue
            record_anchor(row['description'], merchant,
                          count=int(row['confirmations']))

        self.exact = _unique(exact)
        self.stable = _unique(stable)
        self.equivalent = _unique(equivalent)
        self.ambiguous_equivalent = {key for key, values in equivalent.items() if len(values) > 1}
        self.ambiguous_stable = {key for key, values in stable.items() if len(values) > 1}
        self.anchor_owners = anchor_owners
        self.names = names
        self.anchors = {}
        for anchor, by_merchant in anchors.items():
            if len(by_merchant) == 1 and len(anchor_owners[anchor]) == 1:
                example = next(iter(by_merchant.values()))
                if len(example['rules']) >= 2 or example['manual_count'] >= 3:
                    self.anchors[anchor] = example['merchant']

    def match(self, description):
        # All MobilePay transfers/payments use the payment method by default,
        # independently of the recipient. Explicit manual choices on individual
        # rows are protected by merchant_manual=1 in the caller.
        tokens = _tokens(description)
        if tokens and tokens[0] == 'mobilepay':
            return 'MobilePay'

        title = transaction_title(description)
        if title in self.exact:
            return self.exact[title]
        stable = transaction_rule_title(description)
        if stable in self.stable:
            return self.stable[stable]
        if stable in self.ambiguous_stable:
            return None

        signature = _signature(description)
        if signature is None:
            return None
        if signature in self.equivalent:
            return self.equivalent[signature]
        if signature in self.ambiguous_equivalent:
            return None
        kind, words = signature
        if kind != 'purchase':
            return None
        anchor = words[0]
        if anchor in self.anchors:
            return self.anchors[anchor]

        matched = [(key, name) for key, name in self.names.items()
                   if _brand_matches(words, name, description)]
        if len(matched) != 1:
            return None
        owner, name = matched[0]
        # An explicitly assigned *different* merchant for the same bank brand
        # is evidence of ambiguity. Do not override it with a generic guess.
        if any(other != owner for other in self.anchor_owners.get(anchor, ())):
            return None
        return name


def load_merchant_matcher(db, source):
    """Learn from all saved rules and historic manual labels in this source."""
    return MerchantMatcher(
        db.execute('SELECT title,merchant FROM merchant_rules WHERE source=?',
                   (source,)).fetchall(),
        db.execute(
            '''SELECT t.description,t.merchant,COUNT(*) confirmations
               FROM transactions t JOIN accounts a ON a.id=t.account_id
               WHERE a.source=? AND t.amount<0 AND t.merchant_manual=1
                 AND t.merchant IS NOT NULL AND trim(t.merchant)!=''
               GROUP BY t.description,t.merchant''', (source,)).fetchall())


def apply_learned_merchants(db, source):
    """Backfill safe matches without overwriting users' individual selections."""
    matcher = load_merchant_matcher(db, source)
    updates = []
    for row in db.execute(
        '''SELECT t.id,t.description,t.merchant FROM transactions t
           JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND t.merchant_manual=0
             AND (t.amount<0 OR lower(trim(t.description)) LIKE 'mobilepay%')''', (source,)):
        merchant = matcher.match(row['description'])
        if merchant is not None and row['merchant'] != merchant:
            updates.append((merchant, row['id']))
    db.executemany(
        'UPDATE transactions SET merchant=?,merchant_manual=0 WHERE id=?', updates)
    return len(updates)
