# Tee Time Watcher

Watches Hendricks Field, Weequahic, and Francis A. Byrne for tee times that
open up when someone cancels, and pushes an alert to your phone the moment
one matches what you're looking for.

Popular weekend slots are gone the day they're released; they come back only
as cancellations, at random, and get taken again within minutes. This polls
every 30 seconds so you find out immediately instead of by refreshing the
booking page all week.

**It notifies — it does not book.** No course login, no saved card, no
credentials of any kind. Tapping the push opens that course's booking page so
you finish the reservation yourself.

## How it works

All three courses run on one ForeUp booking site, and its
`api/booking/times` endpoint is the same one the public booking page calls —
with an empty `api_key`, so no authentication is involved. The poller asks it
for each course and date you care about, keeps track of what it has already
seen, and pushes to [ntfy.sh](https://ntfy.sh) when something new matches.

| Course | course_id | schedule_id | booking_class |
|---|---|---|---|
| Hendricks Field | 22526 | 11075 | 49493 |
| Weequahic | 22527 | 11077 | 49424 |
| Francis A. Byrne | 22528 | 11078 | 49771 |

## What earns an alert

A slot has to satisfy **every** criterion on one of your watches:

- **Courses** — one, two, or all three.
- **Time window** — earliest/latest, or leave blank for any time.
- **Players** — minimum open spots, e.g. "2 or more". Blank means any.
- **Holes** — 9, 18, or any.
- **Days** — which weekdays, over a look-ahead horizon (default 14 days), or
  one specific date.

Alerts are deduplicated so you get pinged about a genuine opening, not about
the same slot every 30 seconds:

- A slot you haven't seen before → alert immediately.
- A slot that's still open → quiet, then one reminder after
  `RENOTIFY_AFTER_MINUTES` (default 10) in case you missed the first.
- A slot that disappears (someone booked it) and later comes back → treated
  as a fresh opening, alert again.
- A push that fails to send is retried next cycle rather than being recorded
  as delivered.

## Running it locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then edit: GUI_PASSWORD, SECRET_KEY, NTFY_TOPIC
.venv/bin/uvicorn app.main:app --reload
```

Open http://localhost:8000, log in, and create a watch.

For an always-on setup on Lightsail, see [deploy/DEPLOY.md](deploy/DEPLOY.md).

## Configuration

Everything is set through `.env` (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `GUI_PASSWORD` | — | Password for the web GUI. Required. |
| `SECRET_KEY` | — | Signs the session cookie. Required. |
| `NTFY_TOPIC` | — | ntfy topic to push to. **Pick something unguessable** — anyone who knows a topic name can read it. |
| `NTFY_SERVER` | `https://ntfy.sh` | Use your own ntfy server if you'd rather. |
| `POLL_INTERVAL_SECONDS` | `30` | How often to check. |
| `RENOTIFY_AFTER_MINUTES` | `10` | Reminder cadence for a still-open slot. |
| `REQUEST_DELAY_SECONDS` | `0.5` | Spacing between requests inside a cycle. |
| `DB_PATH` | `teetimes.db` | SQLite file. |

### A note on polling load

One watch covering all three courses over both weekend days for two weeks is
about 12 requests per cycle. At the default 30s interval with 0.5s spacing
that's a steady trickle rather than a burst, and comparable to leaving the
booking page open and refreshing. If you add many watches, raise
`POLL_INTERVAL_SECONDS` rather than stacking cycles — and keep in mind that
automated access to a booking site is the kind of thing a course can decide
it doesn't want, whatever the endpoint allows today.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

The suite covers the response parser, the matching rules, and the
notify/dedup logic. It never touches the network.

⚠️ The bundled fixture is **synthetic** — its field names were read off the
booking page's own JS templates, but this was built in a sandbox with no
route to `foreupsoftware.com`, so no live response was ever captured. Before
trusting it, run this from a machine with normal internet access:

```bash
python3 scripts/capture_sample.py weequahic 2026-09-05
```

It saves the real response and prints the fields on the first entry. If they
differ from what `app/foreup_client.py` expects, that's the one place to
adjust. See `tests/fixtures/README.md`.

## Layout

```
app/
  config.py         course IDs and settings
  models.py         Watch (the criteria + matching) and TeeTimeSlot
  foreup_client.py  builds the request, normalizes the response
  poller.py         the 30s loop: fetch, filter, dedup, notify
  notify.py         ntfy push
  db.py             SQLite
  main.py           FastAPI routes
  auth.py           password-gated session cookie
scripts/
  capture_sample.py grab a real API response
deploy/             systemd unit + Lightsail instructions
```

## Possible next steps

- **Auto-booking.** Deliberately not built: it needs your ForeUp login and a
  card on file, is fragile to any change in their checkout flow, and is far
  more likely to get an account flagged than passive polling. If you decide
  you want it, the honest version is a headless-browser script driving the
  real booking form, kept separate from this service.
- Quiet hours, so a 5am cancellation doesn't wake you.
- Per-watch notification targets, if you want to share a watch with whoever
  you're playing with.
