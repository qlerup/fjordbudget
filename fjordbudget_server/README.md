# FjordBudget Server

Separate service for a dedicated unprivileged LXC. It has its own SQLite database,
admin login, moderation queue and contributor API. No bank connections, account
balances, amounts, dates, categories or FjordHub user IDs are accepted. The service
uses reviewed rules, not a generative model. New associations always require review;
known approved associations and spelling variants of the same merchant name merge
automatically. Conflicting names never replace an approved association automatically.

## Install in a new LXC (native, without Docker)

Create an unprivileged Debian 13 LXC with 1 CPU, 512 MB–1 GB RAM and 4 GB disk.
Assign a stable address and a DNS name. The commands below run **inside the new
container** as root. They do not create or change an existing Proxmox container.

```sh
apt update
apt install -y python3 python3-venv git caddy
git clone https://github.com/qlerup/fjordbudget.git /opt/fjordbudget-server
cd /opt/fjordbudget-server
python3 -m venv .venv
.venv/bin/pip install -r fjordbudget_server/requirements.txt
useradd --system --home /var/lib/fjordbudget-server --shell /usr/sbin/nologin fjordbudget-server
install -d -o fjordbudget-server -g fjordbudget-server -m 0700 /var/lib/fjordbudget-server
install -m 0600 fjordbudget_server/server.env.example /etc/fjordbudget-server.env
.venv/bin/python -c 'from getpass import getpass; from werkzeug.security import generate_password_hash; print(generate_password_hash(getpass("Admin password: ")))'
```

Edit `/etc/fjordbudget-server.env`: set `SERVER_URL` to the public HTTPS origin,
paste the generated hash as `ADMIN_PASSWORD_HASH`, retain
`DATA_DIR=/var/lib/fjordbudget-server`. The plain password is never written to disk.
In a systemd environment file the hash is literal, including `$` characters.

```sh
install -m 0644 fjordbudget_server/fjordbudget-server.service /etc/systemd/system/
# Replace the example domain in this file with your DNS name:
install -m 0644 fjordbudget_server/Caddyfile.example /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now fjordbudget-server
systemctl restart caddy
systemctl status fjordbudget-server --no-pager
journalctl -u fjordbudget-server -n 50 --no-pager
```

Only 80/443 need to reach Caddy; the service itself binds to loopback port 8088.
Caddy issues a certificate when DNS and inbound access are configured correctly.
Use your existing HTTPS proxy instead if desired, preserving the original Host
header. No privileged LXC features, Docker nesting or host-mounted data are needed.
Open `https://your-domain/login` and use the admin password you chose.

Print the **public** transport key for configuring participating apps (private keys
stay on the server). Run this as the service user, not root:

```sh
cd /opt/fjordbudget-server
runuser -u fjordbudget-server -- .venv/bin/python -m fjordbudget_server.keys /var/lib/fjordbudget-server
```

Set the output as `COMMUNITY_SERVER_PUBLIC_KEY` in the app's environment. It is a
public key, not a password. Obtain it directly from this server/administrator, not
from an unverified URL. Without the correct pinned key, app sharing cannot be enabled.
Keep `/var/lib/fjordbudget-server/keys/` private and back it up securely. If the
database exists and these keys are missing, startup fails rather than generating
new keys that cannot decrypt existing data.

## Alternative: Docker inside your own suitable container

Native installation above is recommended for LXC. If Docker is already available:

```sh
cp fjordbudget_server/server.env.example server.env
# Generate a hash with the native command above or the built image's Python.
# Set DATA_DIR=/data. For Docker Compose .env syntax, single-quote the hash
# so its dollar signs are not interpolated: ADMIN_PASSWORD_HASH='scrypt:...$...'
docker compose --env-file server.env -f compose.server.yaml build
docker compose --env-file server.env -f compose.server.yaml up -d
docker compose --env-file server.env -f compose.server.yaml exec -T server python -m fjordbudget_server.keys /data
docker compose --env-file server.env -f compose.server.yaml logs --tail 50
docker compose --env-file server.env -f compose.server.yaml down
```

The named `community_data` volume survives `down`. Never use `down -v` for updates.
The compose file binds to localhost by default; proxy it with HTTPS. Only set
`SERVER_BIND` to a LAN address if an external proxy needs to reach port 8088 and the
firewall restricts that port to the proxy. `SERVER_URL` must still be the HTTPS URL.

## Connect FjordBudget installations

Set `COMMUNITY_SERVER_URL=https://your-domain` and `COMMUNITY_SERVER_PUBLIC_KEY=<public-key-output>` in each app's environment and recreate
the app container. Both provided app Compose files pass this optional variable.
It is deliberately not a browser-editable URL. Each owner opens **Indstillinger**,
reads the preview and explicitly enables sharing. It is off by default. A random
installation ID and an encrypted per-installation credential authenticate sync;
these are not bank IDs. Only opted-in credentials can read the shared rule feed.
Contributors do not receive administrator access.

Only remembered rules for manually confirmed outgoing card purchases are eligible.
Demo data and inferred rules are never uploaded. Transfer/salary categories and
MobilePay are excluded. Texts with emails, URLs, long numeric identifiers or IBAN-like
content are excluded. Other texts without a recognized card-purchase prefix are
also excluded conservatively. This filter is **not** a guarantee of anonymization:
bank texts and merchant names can still contain personal names. The app explains
this and shows eligible texts before consent. Review submissions for personal data
before publishing them. Rejected submissions remain admin-only in the database;
restrict admin access and manage retention/backups accordingly.

Sync runs every 10 minutes and can be requested in settings. Only approved exact
bank-text mappings are shared. Local manual choices and private remembered rules
take priority. Downloaded rules are isolated from private training data and are
not uploaded again. Local bank matching does not require the server to be online.

## Moderation and names returned to contributors

Open **Til godkendelse → Godkend / merge**. The modal asks what to name the merchant.
Choose an existing name to merge or type a new one. A differing current shared name
requires a separate replacement checkbox. Incoming duplicate contributions count
once per installation. First/last receipt dates and contributor counts are visible.

The modal also offers an explicit existing-merchant merge target. You may select
that merchant and give the combined result a different canonical name. After the
replacement confirmation, all approved bank texts of the selected merchant are
renamed together, with individual feed revisions so other installations update too.

When sharing is enabled for an existing installation, its entire eligible saved
library is uploaded in batches before any shared rules or name decisions are
downloaded. Existing local names remain in use during upload and pending review.
Pending and rejected submissions never replace local names. Settings show the
remaining upload count and the number awaiting review. Large decision queues are
checked in bounded rotating batches; ordinary libraries are checked each sync.

The contributor receives your approved name on the next sync. The app updates the
submitted remembered rule and transactions with that exact bank text and its previous
name, including the original manually confirmed purchase. A newer local rule or a
different individual transaction name is preserved. A newer local rule becomes a
new proposal and is not overwritten while awaiting review. Imported global rules
never override unrelated private choices. **Rediger / merge** can later change an
approved name. **Træk tilbage** publishes a tombstone to remove the shared cache
entry on each installation; it does not delete users' own saved rules.

Turning sharing off immediately disables local shared matching and removes the
downloaded cache. The credential is withdrawn from the server when reachable, with
retry if offline. Previously accepted contributions and canonical names already
adopted into the user's saved rules remain. Re-enabling requires consent again.

## Backup and updates

Stop the service briefly before copying its complete data directory (SQLite WAL
and session key included), or use SQLite's online backup API. Encrypt and restrict
backups. For native LXC:

```sh
systemctl stop fjordbudget-server
tar -C /var/lib -czf /root/fjordbudget-server-backup.tgz fjordbudget-server
cd /opt/fjordbudget-server
git pull --ff-only origin main
.venv/bin/pip install -r fjordbudget_server/requirements.txt
systemctl start fjordbudget-server
```

For Docker, back up the volume and rebuild with the same Compose file. For rollback,
stop the service, restore the matching backup and previous commit, then restart.
Do not remove the data directory during updates. The admin password is separate in
the environment file; back that up securely too. Audit rows record review actions
without copying bank text into logs. No credentials are included in API status.

## Protocol v1

The logical operations below are transported exclusively as encrypted
`POST /v1/exchange` messages. Unsealed direct `/v1/*` requests are rejected. The
installation credential, operation, query and body are inside the ciphertext;
proxies do not receive a plaintext Authorization header or banking payload. The
server's X25519 public key is pinned in each app's environment. Fresh ephemeral
X25519 keys and HKDF-SHA256 derive separate request/response AES-256-GCM keys.
Authenticated direction/request context, random nonces, a five-minute timestamp
window and a persistent replay guard prevent modification/reuse. Responses are
encrypted too. This is app-to-server encryption, not a peer-to-peer network. It
does not hide connection metadata or protect against the server administrator.

Bank texts and merchant names are Fernet-encrypted at rest, with keyed HMAC indexes
for matching and deduplication. Private keys are stored separately from SQLite in
`DATA_DIR/keys`, mode 0600. Receipt timestamps, moderation status and counts are
metadata, not encrypted fields. The admin UI decrypts data after login and must be
served over HTTPS. Database backups without keys cannot restore the data; anyone
holding both a backup and its keys can decrypt it. Protect complete backups.
Transport-key rotation requires a coordinated update of pinned public keys in the
apps; no automatic trust-on-first-use or plaintext fallback is implemented.

The cryptographic primitives come from PyCA Cryptography, following its
[X25519/HKDF](https://cryptography.io/en/stable/hazmat/primitives/asymmetric/x25519/),
[AEAD](https://cryptography.io/en/stable/hazmat/primitives/aead/) and
[Fernet](https://cryptography.io/en/stable/fernet/) interfaces. The application
envelope is project-specific and is not a separately audited protocol.

All logical contributor operations require the installation secret inside the envelope.
`POST /v1/enroll` accepts only `client_id` (random UUID) and `consent_version`.
`POST /v1/contributions` accepts at most 25 `{bank_text, merchant}` pairs, strictly
rejecting extra fields. `GET /v1/rules?after=<revision>` returns up to 200 changed
approved mappings or revocations. `POST /v1/resolutions` returns moderation decisions
and canonical names for that installation's submissions. `POST /v1/withdraw` revokes
read/write access. Enrollment is a consent declaration, not proof that data is useful;
moderation and rate limits protect the shared knowledge. HTTPS is required by apps.

Admin requests use a separate cookie session and CSRF tokens. API credentials
cannot access the admin UI. Use one Gunicorn worker as shipped (threads are supported);
SQLite transactions enforce idempotency and unique mappings. Requests and responses
are bounded; rate limits are stored in the database. No bank texts, access tokens or
incoming payloads should be enabled in reverse-proxy or application request logging.
