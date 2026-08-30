# Deploying to Lightsail

A `$5/month` Ubuntu instance (1 GB RAM) is more than enough — this is one
Python process polling a JSON endpoint and writing to SQLite.

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

## 6. Lock down access

In the Lightsail console → your instance → **Networking** → IPv4 Firewall,
add a **Custom TCP 8000** rule and set *Restrict to IP address* to your home
IP. The GUI password is the primary gate; the firewall rule means a scanner
never even reaches the login page.

Leave port 8000 closed to the world otherwise — don't add an open 0.0.0.0/0
rule for it.

Now open `http://<your-instance-ip>:8000`, log in, and create a watch.

## Optional: HTTPS on a domain

Traffic to port 8000 is plain HTTP, so your GUI password crosses the network
in the clear. If you'd rather not restrict by IP, put Caddy in front — it
gets a Let's Encrypt cert automatically:

```bash
sudo apt install -y caddy
echo "teetimes.example.com {
    reverse_proxy localhost:8000
}" | sudo tee /etc/caddy/Caddyfile
sudo systemctl restart caddy
```

Then open 80/443 in the Lightsail firewall and close 8000.

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
