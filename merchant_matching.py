"""Conservative, explainable merchant recognition from confirmed bank-text rules."""
import re
import unicodedata
from collections import defaultdict

from db import transaction_title, transaction_rule_title

# Prefixes supplied by banks/card processors, not actual merchants.
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
        # A card-terminal ID is metadata, not part of the merchant's name.
        if tokens and tokens[0].isdigit():
            tokens = tokens[1:]
    # A number-only or non-descriptive label isn't enough evidence.
    if not tokens or not any(any(c.isalpha() for c in token) for token in tokens):
        return None
    return ('transfer' if transfer else 'purchase', tuple(tokens))


def _unique(mapping):
    return {key: next(iter(values)) for key, values in mapping.items() if len(values) == 1}


class MerchantMatcher:
    """Exact > existing stable rule > equivalent text > corroborated brand anchor.

    Only user-confirmed remembered rules are training examples; inferred rows do
    not teach the matcher. Conflicts always disable the broader match.
    """

    def __init__(self, rule_rows):
        exact, stable, equivalent = defaultdict(set), defaultdict(set), defaultdict(set)
        anchors = defaultdict(lambda: defaultdict(set))
        anchor_owners = defaultdict(set)
        for row in rule_rows:
            title, merchant = row['title'], row['merchant']
            exact[transaction_title(title)].add(merchant)
            stable[transaction_rule_title(title)].add(merchant)
            signature = _signature(title)
            if not signature:
                continue
            kind, tokens = signature
            # Short generic fragments are not reliable by themselves.
            if len(tokens) >= 2 or (len(tokens) == 1 and len(tokens[0]) >= 5 and tokens[0].isalpha()):
                equivalent[signature].add(merchant)
            anchor = tokens[0]
            if kind == 'purchase' and len(anchor) >= 4 and anchor.isalpha():
                anchor_owners[anchor].add(merchant)
            merchant_tokens = _tokens(merchant)
            brand_matches = anchor in merchant_tokens or anchor == ''.join(merchant_tokens)
            if (kind == 'purchase' and len(anchor) >= 4 and anchor.isalpha()
                    and anchor not in _GENERIC_ANCHORS and brand_matches):
                anchors[anchor][merchant].add(signature)

        self.exact = _unique(exact)
        self.stable = _unique(stable)
        self.equivalent = _unique(equivalent)
        self.ambiguous_equivalent = {key for key, values in equivalent.items() if len(values) > 1}
        self.ambiguous_stable = {key for key, values in stable.items() if len(values) > 1}
        self.anchors = {}
        for anchor, by_merchant in anchors.items():
            # At least two distinct store/name suffixes must confirm the brand.
            if len(by_merchant) == 1 and len(anchor_owners[anchor]) == 1:
                merchant, examples = next(iter(by_merchant.items()))
                if len(examples) >= 2:
                    self.anchors[anchor] = merchant

    def match(self, description):
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
        kind, tokens = signature
        return self.anchors.get(tokens[0]) if kind == 'purchase' else None


def apply_learned_merchants(db, source):
    """Apply confirmed rules to non-manual entries, without deleting prior labels.

    Never clear a merchant merely because no sufficiently confident rule matches:
    older imported/demo entries can legitimately have labels without a rule.
    """
    matcher = MerchantMatcher(db.execute(
        'SELECT title,merchant FROM merchant_rules WHERE source=?', (source,)))
    updates = []
    for row in db.execute(
        '''SELECT t.id,t.description,t.merchant FROM transactions t
           JOIN accounts a ON a.id=t.account_id
           WHERE a.source=? AND t.amount<0 AND t.merchant_manual=0''', (source,)):
        merchant = matcher.match(row['description'])
        if merchant is not None and row['merchant'] != merchant:
            updates.append((merchant, row['id']))
    db.executemany(
        'UPDATE transactions SET merchant=?,merchant_manual=0 WHERE id=?', updates)
    return len(updates)
