# DialFlow Pro — Predictive Autodialer System

Clean Django + Asterisk autodialer. WebSocket-first. Zero JS polling. ARI worker auto-starts.

---

## Stack

| Layer | Technology |
|---|---|
| Backend | Django 5, DRF |
| Real-time | Django Channels 4 + Redis |
| Task queue | Celery + Redis |
| Database | PostgreSQL 16 |
| Telephony | Asterisk 18+ (ARI + PJSIP realtime) |
| Softphone | JsSIP (WebRTC) |
| Process mgr | Supervisor (production) |

---

## Quick Start (Development)

```bash
# 1. Clone and set up Python env
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env — set DB credentials, Redis URL, Asterisk host/credentials

# 3. Database
createdb dialflow_db
python manage.py migrate

# 4. Create admin user
python manage.py createsuperuser

# 5. Seed initial data (dispositions, sample campaign, phone extension)
python manage.py setup_initial_data

# 6. Start services (3 terminals)
python manage.py runserver        # ARI worker auto-starts here
celery -A dialflow worker -l info
celery -A dialflow beat -l info
```

Open http://localhost:8000

---

## Asterisk Setup

Copy the files from `asterisk/` to your Asterisk server:

```bash
# 1. Copy config files
sudo cp asterisk/pjsip.conf      /etc/asterisk/
sudo cp asterisk/extensions.conf /etc/asterisk/
sudo cp asterisk/ari.conf        /etc/asterisk/
sudo cp asterisk/http.conf       /etc/asterisk/
sudo cp asterisk/extconfig.conf  /etc/asterisk/

# 2. Configure ODBC (see asterisk/res_odbc.conf for instructions)
sudo apt install unixodbc odbc-postgresql
# Edit /etc/odbc.ini and /etc/odbcinst.ini as described in res_odbc.conf
sudo cp asterisk/res_odbc.conf   /etc/asterisk/

# 3. Enable realtime module
echo "load => res_config_odbc.so"  | sudo tee -a /etc/asterisk/modules.conf
echo "load => res_odbc.so"         | sudo tee -a /etc/asterisk/modules.conf
echo "load => res_pjsip_config_wizard.so" | sudo tee -a /etc/asterisk/modules.conf

# 4. Restart Asterisk
sudo systemctl restart asterisk

# 5. Verify realtime is working
asterisk -rx "pjsip show endpoints"
```

The `ps_endpoints`, `ps_auths`, and `ps_aors` tables in `dialflow_db` are created automatically by Django migrations. Asterisk reads them directly — no `pjsip reload` needed when you add an agent phone extension.

---

## Key Architecture Decisions

### ARI Worker — no management command
The ARI WebSocket worker starts automatically inside `TelephonyConfig.ready()` when Django boots. It runs in a daemon thread with an asyncio event loop and reconnects automatically on disconnect.

### Agent Status — always DB-driven
`AgentStatus` is the single source of truth. Every status change writes to the DB first, then broadcasts via WebSocket. The supervisor dashboard and predictive dialer both read from `AgentStatus` — never from JS-side virtual state.

### WebSocket — zero polling
All real-time updates flow through Django Channels. The agent dashboard connects to `/ws/agent/`, the supervisor connects to `/ws/supervisor/`. Nothing polls. The `ReconnectingWS` class in `app.js` handles reconnects with exponential backoff.

### Predictive Dialing
The Erlang-C algorithm runs every second via Celery beat (`predictive_dial_tick`). It reads live agent counts and rolling call metrics from PostgreSQL, calculates the optimal dial ratio, and originates calls via ARI REST. The hopper (Redis list) buffers the next N leads so origination is instant.

### Auto-wrapup
Configured per campaign in the admin. When enabled, `check_wrapup_timeouts` (Celery task, every 5s) detects expired wrapup timers and applies the default disposition server-side. The agent gets a `wrapup_expired` WebSocket event and is returned to ready state. Countdown warnings (`wrapup_timeout_warning`) are pushed at 15s and 10s remaining.

---

## Production Deployment

```bash
# Install Supervisor config
sudo cp deploy/supervisor.conf /etc/supervisor/conf.d/dialflow.conf
sudo supervisorctl reread && sudo supervisorctl update

# Install Nginx config
sudo cp deploy/nginx.conf /etc/nginx/sites-available/dialflow
sudo ln -s /etc/nginx/sites-available/dialflow /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# Collect static files
python manage.py collectstatic --no-input

# Set environment
export DJANGO_SETTINGS_MODULE=dialflow.settings.prod
```

---

## Agent Workflow

1. Agent logs in → browser opens `/agents/`
2. JsSIP registers with Asterisk over WebSocket
3. Agent clicks **Ready** → `AgentStatus.status = 'ready'` written to DB
4. Predictive dialer detects available agent → dials from hopper via ARI
5. Customer answers → ARI bridges customer to agent → agent sees lead info via WebSocket
6. Call ends → `call_ended` WS event → disposition modal opens automatically
7. Agent selects disposition → `CallDisposition` saved → agent returns to **Ready**

---

## File Structure

```
dialflow/
├── dialflow/           # Project config (settings, urls, asgi, celery)
├── core/               # Shared models, WS utils, middleware, routing
├── users/              # Custom User with role system
├── telephony/          # AsteriskServer, Phone, Carrier + ARI worker
├── campaigns/          # Campaign, Disposition, Hopper, Predictive dialer
├── leads/              # Lead, LeadAttempt, CSV importer
├── agents/             # AgentStatus, AgentConsumer (WS), auto-wrapup tasks
├── calls/              # CallLog — permanent call record
├── reports/            # DailySnapshot, reporting views
├── templates/          # Django MVT templates
├── static/             # CSS + JS (app.js, main.css)
├── asterisk/           # Asterisk config files
└── deploy/             # Supervisor + Nginx configs
```
