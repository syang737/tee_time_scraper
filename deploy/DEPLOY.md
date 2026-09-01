# Deploying to Lightsail

A `$5/month` Ubuntu instance (1 GB RAM) is more than enough — this is one
Python process polling a JSON endpoint and writing to SQLite.

## Quick start

Create the instance (Lightsail → Linux/Unix → **Ubuntu 22.04 LTS** → $5 plan),
give it a **static IP**, point a DNS `A` record at that IP, then:

```bash
ssh -i LightsailKey.pem ubuntu@<your-static-ip>
git clone https://github.com/syang737/tee_time_scraper.git
cd tee_time_scraper
./deploy/setup.sh golf.potpourri.lol
```

That installs Python and Caddy, creates the venv, generates `.env` with a
random password and ntfy topic, installs and starts the systemd service, gets
a TLS certificate, caps the journal, and then verifies the whole thing. It
prints the generated password and topic at the end.

The one thing it cannot do is open the firewall — that lives in the AWS
console. Lightsail → your instance → **Networking** → IPv4 Firewall: add
**HTTP (80)** and **HTTPS (443)**, and leave **8000 closed**. Port 80 is
required, since Let's Encrypt validates over it.

Two other things worth knowing:

```bash
./deploy/setup.sh --check          # verify an install, change nothing
git pull && ./deploy/setup.sh golf.potpourri.lol   # update in place
```

Re-running is safe: it never overwrites an existing `.env`.

> **`.env` is only read when the service starts.** Editing it does nothing
> until `sudo systemctl restart teetimes` — a running process's environment
> is fixed at launch. `--check` compares the two and tells you when they have
> drifted apart.

The rest of this document is the same steps done by hand, which is worth
reading if something goes wrong or you want to deviate.

## 1. Create the instance

Lightsail → Create instance → Linux/Unix → **Ubuntu 22.04 LTS** → the $5 plan.
Download the SSH key, then connect:

```bash
ssh -i LightsailKey.pem ubuntu@<your-instance-ip>
```

## 2. Install and clone

```bash
sudo apt update && sudo apt install -y python3-venv git
git clone https://github.com/syang737/tee_time_scraper.git
cd tee_time_scraper
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

## 3. Configure

```bash
cp .env.example .env
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # for SECRET_KEY
python3 -c "import secrets; print('teetimes-' + secrets.token_urlsafe(12))"  # for NTFY_TOPIC
nano .env
```

Set at minimum `GUI_PASSWORD`, `SECRET_KEY`, and `NTFY_TOPIC`.

**Pick an unguessable ntfy topic.** ntfy.sh topics are public to anyone who
knows the name — a random suffix is what keeps your alerts yours.

Then install the **ntfy** app on your phone (iOS/Android) and subscribe to
that exact topic.

## 4. Confirm the API is reachable from the box

Before running the service, check that the course API answers and that the
parser understands it:

```bash
.venv/bin/python scripts/capture_sample.py weequahic 2026-09-05
```

It prints the parsed slots and the field names on the first entry. If it
prints slots, you're good. If the field names differ from what the parser
expects, see `tests/fixtures/README.md`.

## 5. Run it as a service

```bash
sudo cp deploy/teetimes.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now teetimes
systemctl status teetimes
journalctl -u teetimes -f      # watch the poller live
```

The unit file assumes the repo lives at `/home/ubuntu/tee_time_scraper` and
runs as `ubuntu`. Edit the paths if yours differ.

## 6. Serve it on a subdomain over HTTPS

The app binds to `127.0.0.1` (see the unit file), so it is not reachable from
outside the box at all. Caddy terminates TLS in front of it.

**Why a subdomain rather than a path** like `example.com/golf`: every link,
form action and redirect in the app is rooted at `/`. Serving it under a path
prefix would need `root_path`, prefix-aware templates and a scoped cookie.
A subdomain needs none of that, and keeps this deployment independent of
whatever else the apex domain serves.

### Point DNS at the instance

Give the instance a **static IP** (Lightsail → Networking → Create static IP;
without one the address changes on restart). Then add one record at your DNS
provider — wherever the domain's nameservers point, which may not be the same
place the apex site is hosted:

```
Type: A    Name: golf    Value: <your-static-ip>    TTL: 300
```

Confirm it resolves before continuing, or Caddy's certificate request will
fail:

```bash
dig +short golf.potpourri.lol
```

### Open the firewall

Lightsail → your instance → **Networking** → IPv4 Firewall:

- **Add** HTTP (80) and HTTPS (443), open to everyone. Port 80 is required —
  Let's Encrypt validates over it.
- **Do not** open 8000. Caddy reaches the app over localhost.

### Install Caddy

Caddy is not in Ubuntu's default repositories, so **run this whole block** —
`apt install caddy` on its own will report "Unable to locate package".

On a freshly created instance apt is often already busy with the automatic
first-boot updates, and you'll get `Could not get lock
/var/lib/dpkg/lock-frontend`. Wait for it rather than removing the lock or
killing the process, either of which can leave dpkg half-configured:

```bash
while sudo fuser /var/lib/dpkg/lock-frontend >/dev/null 2>&1; do
  echo "waiting for the automatic updater..."; sleep 5
done
```

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy

sudo cp deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile      # set your hostname
sudo systemctl reload caddy
```

Caddy obtains and renews the certificate by itself. Watch it happen with
`journalctl -u caddy -f`.

### Tell the app it is behind HTTPS

Add to `.env`, so the session cookie is never sent over a plain connection:

```
COOKIE_SECURE=true
```

Then `sudo systemctl restart teetimes` and open
`https://golf.potpourri.lol`.

### A note on who can reach it

Once this is on a public domain the shared password is the only thing
protecting it — the earlier IP restriction is gone. Use a genuinely strong
`GUI_PASSWORD`. The scraper holds no course login or payment details, so the
worst case is someone seeing or editing your watches, but pick a real
password anyway.

If you later move the domain's DNS to Cloudflare for other reasons, a
Cloudflare Tunnel would let you close ports 80/443 entirely and stop
publishing the instance's IP. It is not worth migrating nameservers just for
that.

## Keeping the disk in check

A 20 GB instance is plenty, but a process polling every 30 seconds forever
needs two things bounded. The database is *not* the one to worry about:

| Source | Growth | Bounded by |
|---|---|---|
| `teetimes.db` | ~1 MB/year | `RETENTION_DAYS` (default 5), purged daily |
| systemd journal | ~4 GB/year if unbounded | `SystemMaxUse` below |
| Caddy access log | bounded | `roll_size`/`roll_keep` in the Caddyfile |

**The database** holds one row per matching slot, updated in place rather
than appended, so it only grows as new dates enter the horizon. Anything not
seen for `RETENTION_DAYS` is deleted once a day and the file is `VACUUM`ed so
the space actually returns to the filesystem. Retention is keyed on when a
slot was last *seen*, not on its tee time, so a slot that is still open is
never purged and can't come back as a duplicate alert. The dashboard shows
the current row count and file size.

**The journal** was the real risk. httpx logs a full URL per request at INFO,
which at 12 requests every 30 seconds is roughly 35,000 lines and 11 MB a
day. The app now sets httpx to WARNING, which removes essentially all of it —
failures still get logged by the poller. Cap the journal anyway so nothing
else can fill the disk:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
echo -e "[Journal]\nSystemMaxUse=200M" \
  | sudo tee /etc/systemd/journald.conf.d/size.conf
sudo systemctl restart systemd-journald
```

Check on things any time with:

```bash
df -h /                          # disk overall
du -h ~/tee_time_scraper/*.db    # database
journalctl --disk-usage          # logs
```

## Updating

```bash
cd ~/tee_time_scraper && git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart teetimes
```

## Troubleshooting

- **No alerts arriving** — hit *Send test notification* on the dashboard. If
  that doesn't reach your phone, the problem is the ntfy topic or the app
  subscription, not the scraper.
- **Dashboard shows a "last error"** — the poller reports when every request
  for a watch failed. `journalctl -u teetimes -n 50` has the detail.
- **Service won't start** — `journalctl -u teetimes -n 50`. Usually a wrong
  path in the unit file or a missing `.env`.
