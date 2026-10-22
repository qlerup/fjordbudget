# FjordBudget

![FjordBudget](static/logos/social.png)

A personal budget app with account balances, booked transactions, categories, monthly budgets, spending analysis and savings goals. The interface currently uses Danish labels.

## Install through FjordHub

Choose **FjordBudget**, a port (default **8060**) and data directory. Open it from Hub for SSO or use the same account on the login page. The first administrator to sign in becomes the installation's permanent budget owner. Other users cannot access the owner's data, even with an app role. Use separate installations for independent owners.

Choose the public domain after installation in Hub's app settings. Use a dedicated HTTPS origin; subpath mounting is unsupported. The app retrieves the configured address with a maximum 30-second cache and shows the bank callback URL in Connect bank. Private LAN addresses work too. Forwarded host/protocol headers are not the trust basis.

[fjordhub.json](fjordhub.json) selects `docker-compose.fjordhub.yml`, requiring Hub configuration and current membership. Hub outages block protected requests. First-login email/password setup works inside the app. Removed access invalidates existing sessions. Administrators can centrally require an email update; opening or returning to any updated app presents a mandatory form until a valid, unique address is saved. Completion clears the shared requirement. Update Hub and Budget together.

## Local Docker Desktop demo

```sh
docker compose up -d --build
docker compose logs --tail 100 app
docker compose stop
docker compose start
```

Open `http://localhost:8060`. The demo contains fictional accounts and four months of transactions. My bank data opens a separate area; demo and real data are never mixed.

The app automatically detects its login flow. With both `FJORDHUB_URL` and
`FJORDHUB_API_KEY`, it uses FjordHub; without them, a fresh installation opens
**Første opsætning**, just like FjordLens. Create the first owner's username and
password, then log in. No one-time setup code or separate app edition is needed.
Once the owner exists, setup cannot create another account. An initialization
marker prevents missing owner data from silently reopening registration.

The desktop `compose.yaml` still binds only `127.0.0.1:8060`. Its default also uses
first setup. `AUTH_MODE=local` remains a legacy unauthenticated loopback/test mode;
never use that explicit override for a network deployment. The automatic choice
is the default and requires no `AUTH_MODE` setting. Partial Hub credentials fail
closed rather than unexpectedly enabling local registration.

For a dedicated LXC, see [native app installation](deploy/README.md). This is the
same app and repository as the Hub installation. Each deployment has its own
database, bank credentials, session/encryption keys and owner account.

## Read-only bank access

Enable Banking **Account Information Services (AIS)** reads accounts, balances and booked transactions. No payments are implemented; payment endpoints are rejected even if provider credentials permit them. POST starts consent/exchanges its authorization code; DELETE only disconnects read consent.

1. Create an application in [Enable Banking](https://enablebanking.com/cp/applications).
2. Use Sandbox for tests. Production access to your own accounts depends on provider approval, coverage, terms and any charges; not all banks/accounts are promised.
3. Register the exact callback. The app displays the callback for the address you are using; log in through the final HTTPS domain before registering it. The Hub deployment uses its configured HTTPS callback. Never expose the legacy unauthenticated `AUTH_MODE=local` override through a tunnel.
4. Enter the app ID and complete private RSA PEM key in Connect bank and save. No environment change/restart is needed; API credentials can be replaced later.
5. Credentials are encrypted in `/data/bank-credentials.enc`, with restricted permissions, and never returned to the browser. Saved credentials override optional `ENABLE_BANKING_APP_ID` and `secrets/enablebanking.pem` configuration.
6. Locally, use `localhost` rather than `127.0.0.1` so the callback cookie follows the redirect.
7. Select the bank and approve read access there. MitID and bank login take place outside FjordBudget.

Each new consent's first synchronization requests the **longest available history** (`strategy=longest`); the bank determines the actual range. A completion modal reports the oldest returned booking and resulting history. Later refreshes request the last 90 days when the previous fetch is at least 30 minutes old. Imported history remains, and Update balances works manually at any time.

Reconnect expired consent. A stable account hash preserves accounts, history and manual categories. Disconnect bank access revokes provider access and stops imports while retaining local data. Controlled provider responses validate the implementation; a live approval/import depends on actual credentials and consent.

References: [API](https://enablebanking.com/docs/api/reference/), [FAQ](https://enablebanking.com/docs/faq/), [quick start](https://enablebanking.com/docs/api/quick-start/alt).

## Transactions, categories and budgets

- Amounts are integer minor currency units, not floating-point values.
- DKK, EUR, SEK, NOK, GBP, USD, CHF and PLN are supported and summed separately without conversion.
- Balance is the latest retrieved bank balance, preferably booked balance, not a month-end forecast.
- Budgets are monthly category spending ceilings. Automatic proposals use up to 12 complete previous months, add 5% to average spending and round up to DKK 50. Review/edit before saving. Unmapped categories appear separately.
- Create, rename and delete categories with a replacement; map them to budget categories. New categories ask whether a merchant is required, and the setting remains editable. Without that requirement, transactions use the original bank description without forcing merchant selection.
- Transfers are excluded from income/expense analysis; check categorization, especially transfers between your own accounts.
- Deduplication uses `entry_reference` per account, otherwise date, amount, currency, text and occurrence number. Identical purchases remain distinct; later bank changes without stable references may need reconciliation.
- Pending/reserved transactions are not included, so displayed totals can differ from the bank balance.
- All pages are fetched before saving an account's import. An account failure preserves history and does not stop other accounts updating.

## Spending analysis and savings goals

Set merchants manually and select Remember for future entries. Rules match the normalized complete bank description; card abbreviations alone are not guessed. Original descriptions remain intact.

Goals have a target, saved amount and deadline. Optionally link an account to use its latest balance; the manual amount is retained if unlinked. One active goal appears on the overview. Monthly saving requirements are compared with historical disposable income and classified as realistic, tight, possible with changes, or unrealistic under current finances.

Savings suggestions are scenarios for completely stopping purchases at a learned merchant, not budget categories or a universal one-fewer-visit rule. Fixed-expense merchants and one-off purchases are excluded. The potential amount is the lower of historical and recent monthly average spending; it is only an actual saving if the spending is not transferred to another merchant.

Suggestions use up to the last 12 completed calendar months on the goal's selected analysis accounts, independently of the page's date range. If history is shorter, the baseline starts in the first available month; intervening months with no purchases still count in the average. The current month is excluded. A comparison with the last three completed months removes merchants without recent purchases and caps each scenario at the recent monthly average. Merchants with a spending reduction of more than 20% rank below unchanged habits. The card shows the baseline and comparison dates and states which average is used. The page's selected date range continues to determine its income, expenses and disposable income.

The goal lists the total number of relevant suggestions. The first three are visible initially; “Vis flere forslag” reveals the rest. There is no fixed limit on eligible merchants. The projected date with changes assumes all listed suggestions are followed, including those in the expandable list.

Goal calculations assume this month's deposit is already reflected in the current saved balance. New deposits start next calendar month and include the deadline month. The card shows this calculation period. Required monthly savings, projected completion dates, and each scenario's effect all use this same start month. A goal due this month shows the amount still needed before month-end instead of treating the month as another future deposit.

Each scenario also shows its individual contribution by the goal's deadline, capped at the remaining goal amount, its share of that remaining amount, and any estimated earlier completion month. These effects assume the freed amount is saved each month starting next month; an existing monthly deficit must be covered first. Individual scenarios are assessed separately, so capped progress percentages should not be added together.

Use “Spring over” to choose between excluding a merchant for the current calendar month or permanently for that goal. The next relevant opportunity replaces it when available. Both choices persist across reloads and apply to the goal on the overview too; “Tag med igen” restores a suggestion. Monthly skips expire next month; permanent skips remain until restored. The selected analysis dates still determine the spending history. Nothing automatically changes budgets or transactions.

## Shared merchant knowledge (optional)

FjordBudget Server is a separate service for a dedicated LXC, with its own database,
admin UI and moderation queue. See [server installation and operation](fjordbudget_server/README.md).
Both app Compose files support `COMMUNITY_SERVER_URL=https://your-server` and
`COMMUNITY_SERVER_PUBLIC_KEY` (the server's pinned public transport key).
Each owner explicitly enables sharing in **Indstillinger**, with an explanation and
preview of eligible saved bank-text → merchant pairs. Sharing is off by default.
Only bank text and merchant name are contributed; balances, amounts, dates, accounts
and categories stay private. Conservative filtering excludes transfers and risky
texts, but is not a guarantee of anonymization. Review the preview before enabling.

Participants can download approved rules while opted in. Their apps only need an
outbound HTTPS connection; no public app address, inbound ports or port forwarding
are required. Unknown/conflicting associations wait for the server owner's review.
The merge dialog asks for a canonical merchant name; that name returns to the original
contributor's saved rule and matching transactions, preserving newer local edits.
Downloaded rules are kept separately and are never uploaded as new training data.
This is a growing reviewed rule library, not an AI model trained on banking histories.
App/server request and response bodies are authenticated and encrypted with the pinned
server key on top of HTTPS; no plaintext fallback is accepted. Bank texts and merchant
names are also encrypted in the server database, with key files stored separately.
The server can decrypt for moderation; this is not a direct peer-to-peer network.

## Data and backup

The local `fjordbudget_budget_data` volume holds SQLite plus encryption/session keys. Bank session IDs and remote account IDs use Fernet; transactions and budget data are not encrypted as a whole database. Protect the disk and backups.

Back up the complete volume, `encryption.key`, `session.key`, `bank-credentials.enc` and any private API key in `secrets/`. Preserve data and keys together privately; the key in the same volume does not protect against access to that entire volume. Stop before a file-based copy. Ordinary stop/rebuild preserves data; `docker compose down -v` deletes it.

Login permits five attempts per five minutes per direct IP (per proxy behind a reverse proxy). Cookies are HttpOnly/SameSite and Secure on the configured HTTPS domain. Writes require CSRF; consent state is browser-bound, single-use and valid for 15 minutes. Host is checked against local operation or the configured origin. No external fonts, CDN scripts, analytics or payment APIs are used.

## Development and operations

Python 3.13, Flask, SQLite and plain HTML/CSS/JavaScript. Use one threaded Gunicorn worker: synchronization locks are process-local.

```sh
python -m pip install -r requirements.txt playwright
python -m playwright install chromium
python -m unittest discover -s tests -v
```

Temporary-database tests cover mobile navigation, categories, budgets, bank setup/errors, duplicates, identical purchases, currency isolation, consent, encrypted tokens and pagination.

Public [privacy](https://qlerup.github.io/fjordbudget/privacy.html) and [terms](https://qlerup.github.io/fjordbudget/terms.html) pages are also available at `/privacy` and `/terms` without login. Adapt them if operator/audience changes. Provider acceptance of localhost legal-page URLs is unverified; publish these pages separately if public URLs are required. GitHub Pages serves only `docs/`, without banking data/credentials/app code. Rebuild with `python scripts/build_public_pages.py`.

Artwork is in `static/logos/` and [branding/README.md](branding/README.md). Use Hub's updater for managed deployment. Preserve the entire data directory and original keys. The owner table migrates without changing bank data. Do not roll an exposed deployment back to an unauthenticated version.
