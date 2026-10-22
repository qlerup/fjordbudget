# FjordBudget in its own LXC

The same GitHub source automatically uses FjordHub login when both Hub credentials
are configured, or first-admin setup otherwise. There is no separate build or
one-time setup code. Follow the same user flow as FjordLens: open the app, create
the first account, then log in. Leave the first real account for its intended owner.

Use an unprivileged Debian 13 LXC, 1 core, 1 GB RAM and 8 GB disk. A Debian 13 LXC
on older Proxmox container profiles may need `nesting=1` for systemd's mount
namespaces; no Docker, privileged LXC, host data mounts or unconfined AppArmor are
required. The app listens on port 8060, and the provided service starts on boot.

Inside the fresh container as root:

```sh
apt update
apt install -y python3 python3-venv git ca-certificates
git clone https://github.com/qlerup/fjordbudget.git /opt/fjordbudget
cd /opt/fjordbudget
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
useradd --system --home /var/lib/fjordbudget --shell /usr/sbin/nologin fjordbudget
install -d -o fjordbudget -g fjordbudget -m 0700 /var/lib/fjordbudget
install -m 0600 deploy/app.env.example /etc/fjordbudget.env
```

Set `APP_URL` to the LAN URL, e.g. `http://192.168.1.100:8060`, and optionally set
`INSTALLATION_NAME` to a human-friendly name. Do **not** set `AUTH_MODE=local`:
that explicit legacy mode deliberately bypasses login and is only for loopback
or isolated tests. No auth-mode flag is needed for automatic detection.

```sh
install -m 0644 deploy/fjordbudget.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now fjordbudget
systemctl status fjordbudget --no-pager
journalctl -u fjordbudget -n 50 --no-pager
```

Open the app and let the owner complete **Første opsætning**. Only fictional demo
accounts exist initially; real bank connections, credentials and shared-text opt-in
are empty/off. The username and password are private to this installation.
Only one owner is supported. Hashes, bank data and keys survive restarts in
`/var/lib/fjordbudget`. Ten local login attempts per five minutes are allowed per
direct address, persisted across process restarts. Setup and login validate CSRF;
ordinary API writes use the existing CSRF protection. Sessions expire after 12 hours.

## A domain via an existing proxy or FjordHub

Point the tunnel/proxy to `http://<LXC-IP>:8060`. Preserve the original Host header.
Add the proxy's exact LAN IP to `TRUSTED_PROXY_IPS` in `/etc/fjordbudget.env`, then
restart the service. Only those addresses can supply `X-Forwarded-Proto: https`.
Forwarded Host is ignored. HTTPS at the public domain is required for banking.

The first setup or a subsequent **successful owner login** confirms the address
being used; untrusted requests cannot change it. This allows a new domain to be
assigned after the initial LAN installation. The configured LAN URL stays usable.
Open and log in through the final domain before copying the banking callback;
the callback keeps the same browser origin so bank consent cookies work correctly.
No FjordHub membership is needed merely because its proxy routes the domain.

## Update, backup and recovery

Stop the service briefly and back up the complete data directory and environment
file, including session/encryption keys and `local-install-initialized`. Restrict
and encrypt backups. Do not copy another user's database or keys into a fresh app.

```sh
systemctl stop fjordbudget
tar -C /var/lib -czf /root/fjordbudget-backup.tgz fjordbudget
cd /opt/fjordbudget
git pull --ff-only origin main
.venv/bin/pip install -r requirements.txt
systemctl start fjordbudget
```

If owner data is missing but the installation marker exists, registration stays
locked. Restore the matching database and keys from backup rather than deleting
the marker or making the installation public again. For rollback, stop the service,
restore the matching backup and previous commit, then start it. Do not remove the
data directory during updates. A complete fresh installation is a separate LXC/data
directory, not a reset of an existing person's budget.
