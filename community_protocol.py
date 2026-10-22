"""Versioned, conservative wire format shared by the app and standalone server."""
import re
import unicodedata

CONSENT_VERSION = '2026-10-23'


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def merchant_key(value):
    return ''.join(char for char in key(value) if char.isalnum())


def safe_pair(bank_text, merchant):
    """Reject risky texts instead of claiming arbitrary bank text is anonymous.

    Only card-purchase descriptions are eligible. Transfer recipients, salary,
    contact details, and long account/card/reference numbers are not published.
    Short shop/terminal codes are retained: removing them could conflate shops.
    """
    if not isinstance(bank_text, str) or not isinstance(merchant, str):
        raise ValueError('Banktekst og forhandlernavn skal være tekst.')
    for value, limit in ((bank_text, 200), (merchant, 100)):
        if not 1 <= len(value.strip()) <= limit or any(unicodedata.category(c).startswith('C') for c in value):
            raise ValueError('Ugyldig tekstlængde eller kontroltegn.')
        compact = re.sub(r'[\s.()+/-]', '', value)
        if ('@' in value or re.search(r'https?://', value, re.I)
                or re.search(r'\d{6,}', compact)
                or re.search(r'\b[A-Z]{2}\d{2}[A-Z0-9 ]{8,}', value, re.I)):
            raise ValueError('Teksten kan indeholde personlige oplysninger eller referencer.')
    text = key(bank_text)
    if not re.match(r'^(mcd|pos|kortkøb|kortkoeb|kortbetaling|dankort|visa|mastercard|debit)\b', text):
        raise ValueError('Kun banktekster fra kortkøb deles. Overførsler og fritekster udelades.')
    if not merchant_key(merchant) or not any(c.isalpha() for c in merchant):
        raise ValueError('Vælg et forhandlernavn.')
    return text, ' '.join(merchant.strip().split())
