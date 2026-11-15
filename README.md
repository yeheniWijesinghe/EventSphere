# EventSphere

A mobile-friendly venue booking and digital ticketing platform where every booking decision is made by cooperating software agents.

* **Backend:** Python 3.11+, FastAPI, Pydantic, SQLAlchemy 2 (PostgreSQL in production, SQLite locally)
* **Agents:** VenueAgent, BookingAgent, TicketAgent, WaitlistAgent, NotificationAgent, plus a **HirerAgent** that negotiates venue hire for the attendee. They talk only through JSON messages on a message bus, using REQUEST, AGREE, REFUSE, PROPOSE, INFORM and FAILURE.
* **Creative extras:** explainable decisions (every reply names its rule and evidence), alternating-offer price negotiation, agent mood/workload with a scarcity rule, and an animated live conversation diagram.
* **Frontend:** Jinja2 + Bootstrap 5, works at 375 px; QR codes with `qrcode`, camera scanning with `html5-qrcode`
* **Payments:** simulated only. No card details are collected.

The agent design and every decision rule are in [`docs/AGENT_RULES.md`](docs/AGENT_RULES.md).

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. A SQLite database (`eventsphere.db`) and demo data are created on first start.
To re-create the demo data with fresh dates (locally), run `python -m app.seed --reset`.

To try the phone camera locally, the page has to be served over HTTPS. Use the deployed site, or a tunnel such as `ngrok http 8000`.

## Demo accounts

| Role | Email | Password |
|------|-------|----------|
| Admin / organiser | admin@eventsphere.lk | Admin@2026 |
| Gate staff | staff@eventsphere.lk | Staff@2026 |
| Attendee | attendee@eventsphere.lk | Attendee@2026 |

(These can be changed with the `DEMO_*` environment variables before the first start.)

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `DATABASE_URL` | `sqlite:///./eventsphere.db` | `postgres://…` URLs from Render are converted automatically |
| `SECRET_KEY` | dev value | Signs the session cookie. **Set a long random value in production** |
| `ENVIRONMENT` | `development` | `production` enables secure (HTTPS-only) cookies |
| `APP_TIMEZONE` | `Asia/Colombo` | Local time used by all agents |
| `WAITLIST_HOLD_MINUTES` | 10 | How long freed seats are held for a waitlisted attendee |
| `SWEEP_INTERVAL_SECONDS` | 30 | WaitlistAgent timer interval |
| `CHECKIN_OPENS_MINUTES` | 180 | Gates open this many minutes before the event starts |
| `ENABLE_SCHEDULER` | 1 | Turn the timer off (tests do) |
| `SEED_DEMO_DATA` | 1 | Create demo accounts/venues/events when the DB is empty |
| `DEMO_ADMIN_EMAIL` / `DEMO_ADMIN_PASSWORD` etc. | see above | Demo account credentials |

Secrets are never committed: `.env` is git-ignored, and `.env.example` documents the variables.

## Tests

```bash
pytest
```

There are 39 tests, including overbooking, two buyers for the last seat at the same moment, double check-in, wrong-event and cancelled tickets, the suspicious-scan alert, VenueAgent clashes, counter-proposals, escalation and pricing, the WaitlistAgent offer, claim and timer expiry, message traceability, beliefs surviving a restart, CSRF, role checks, the web booking flow, and the creative features: rule explanations, the high-demand rule, HirerAgent negotiation (deal, no deal, time flexibility), agent mood and the conversation diagram.

## Deploy to Render

1. Push this repository to GitHub.
2. In Render choose **New → Blueprint** and select the repository. `render.yaml` creates the web service and a free PostgreSQL database, and generates `SECRET_KEY`.
3. Wait for the deploy to finish, then open the `https://…onrender.com` URL.
4. Free instances sleep when idle. **Open the site a few minutes before the viva.** The demo concert is seeded to start about an hour after the first deploy. For the check-in demo, sign in as admin and create an event that starts within the next 3 hours, so the gates are open.

## Main pages

| Path | Who | What |
|------|-----|------|
| `/` | everyone | Event list with keyword, category and advanced search (venue, date range, price range) |
| `/events/{id}` | attendee | Tickets left per category, booking with simulated payment, waitlist |
| `/bookings`, `/bookings/{id}` | attendee | Booking history, QR tickets, cancellation with refund quote |
| `/waitlist` | attendee | Waitlist entries, held-seat countdown, claim |
| `/hire`, `/hire/mine` | attendee | Private venue hire with instant decision, price breakdown and alternatives |
| `/staff/scan` | staff, admin | Camera QR scanner with manual entry fallback |
| `/admin` | admin | Sales, revenue, check-ins, capacity, charts, agent beliefs, suspicious scans |
| `/admin/events`, `/admin/venues`, `/admin/hires` | admin | Management screens |
| `/admin/messages` | admin | Agent message log with performatives and rule IDs, filterable by conversation id |
| `/admin/conversations/{id}` | admin | Animated sequence diagram of one conversation between agents |

## Project layout

```
app/
  agents/        messages.py (Message + performatives), bus.py, base.py, rules.py,
                 venue.py, booking.py, ticket.py, waitlist.py, notification.py, hirer.py
  routers/       auth, public (events/booking), attendee, hire, staff, admin
  templates/     Jinja2 pages (mobile-first)
  static/        style.css
  models.py      SQLAlchemy models   schemas.py  Pydantic form validation
  security.py    PBKDF2 hashing, CSRF, ticket codes   scheduler.py  WaitlistAgent timer
tests/           pytest suite
docs/AGENT_RULES.md
render.yaml
```
