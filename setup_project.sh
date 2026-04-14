#!/bin/bash
# setup_project.sh — Run once after fresh clone / new machine setup
# Works on native Ubuntu (not WSL)

set -e
cd "$(dirname "$0")"

BOLD="\e[1m"
GREEN="\e[32m"
YELLOW="\e[33m"
RED="\e[31m"
RESET="\e[0m"

step() { echo -e "\n${BOLD}${GREEN}▶ $1${RESET}"; }
warn() { echo -e "${YELLOW}  ⚠  $1${RESET}"; }
info() { echo -e "  ✔  $1"; }
fail() { echo -e "${RED}  ✖  $1${RESET}"; exit 1; }

echo -e "\n${BOLD}════════════════════════════════════════"
echo -e "   DialFlow Pro — Project Setup"
echo -e "════════════════════════════════════════${RESET}\n"

# ── System Services ────────────────────────────────────────────────────────────
step "Starting system services..."

sudo systemctl enable --now redis-server     && info "Redis enabled & running" || warn "Redis may need manual start"
sudo systemctl enable --now postgresql       && info "PostgreSQL enabled & running" || warn "PostgreSQL may need manual start"
sudo systemctl enable --now asterisk 2>/dev/null && info "Asterisk enabled & running" || warn "Asterisk not installed or needs manual start"

sleep 1

# Verify Redis
if redis-cli ping | grep -q "PONG"; then
    info "Redis connection: OK"
else
    warn "Redis not responding — Celery/channels will start in memory mode"
fi

# ── Virtual Environment ────────────────────────────────────────────────────────
step "Setting up Python virtual environment..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
    info "venv created"
else
    info "venv already exists"
fi

source venv/bin/activate
info "venv activated (Python: $(python --version))"

# ── Dependencies ───────────────────────────────────────────────────────────────
step "Installing Python dependencies..."
pip install --upgrade pip -q
pip install -r requirements.txt -q
info "Dependencies installed"

# ── SSL Certificates ───────────────────────────────────────────────────────────
step "Checking SSL certificates..."
mkdir -p keys

if [ ! -f "keys/cert.pem" ] || [ ! -f "keys/key.pem" ]; then
    warn "No SSL certs found — generating self-signed cert for development..."
    openssl req -x509 -newkey rsa:4096 -keyout keys/key.pem -out keys/cert.pem \
        -days 365 -nodes \
        -subj "/C=IN/ST=Dev/L=Local/O=DialFlow/CN=localhost" \
        -config <(cat /etc/ssl/openssl.cnf <(printf "\n[SAN]\nsubjectAltName=IP:127.0.0.1,DNS:localhost")) \
        2>/dev/null || \
    openssl req -x509 -newkey rsa:4096 -keyout keys/key.pem -out keys/cert.pem \
        -days 365 -nodes \
        -subj "/C=IN/ST=Dev/L=Local/O=DialFlow/CN=localhost" 2>/dev/null
    info "Self-signed SSL certificate generated (valid 1 year)"
else
    EXPIRY=$(openssl x509 -in keys/cert.pem -noout -enddate 2>/dev/null | cut -d= -f2)
    info "SSL cert found (expires: ${EXPIRY})"
fi

# ── Static Files ───────────────────────────────────────────────────────────────
step "Creating required directories..."
mkdir -p logs media staticfiles static
info "Directories ready: logs/ media/ staticfiles/ static/"

# ── Database Migrations ────────────────────────────────────────────────────────
step "Running database migrations..."
python manage.py migrate --run-syncdb 2>&1 | tail -5
info "Migrations complete"

# ── Initial Data Seed ──────────────────────────────────────────────────────────
step "Seeding initial data..."
python manage.py setup_initial_data
info "Initial data seeded"

# ── Celery Beat Schedule ───────────────────────────────────────────────────────
step "Setting up Celery beat schedule..."
python manage.py setup_beat_schedule 2>&1 | tail -5 || warn "Beat schedule setup skipped"

# ── Static Files Collection ────────────────────────────────────────────────────
step "Collecting static files..."
python manage.py collectstatic --noinput -v 0
info "Static files collected"

# ── Superuser ─────────────────────────────────────────────────────────────────
step "Checking superuser..."
SUPERUSER_EXISTS=$(python manage.py shell -c "from django.contrib.auth import get_user_model; print(get_user_model().objects.filter(is_superuser=True).exists())" 2>/dev/null)

if [ "$SUPERUSER_EXISTS" = "True" ]; then
    ADMIN_USER=$(python manage.py shell -c "from django.contrib.auth import get_user_model; u=get_user_model().objects.filter(is_superuser=True).first(); print(u.username)" 2>/dev/null)
    info "Superuser already exists: ${ADMIN_USER}"
else
    warn "No superuser found. Creating one now..."
    echo ""
    python manage.py createsuperuser
fi

# ── Summary ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}════════════════════════════════════════"
echo -e "   Setup Complete!"
echo -e "════════════════════════════════════════${RESET}"
echo ""
echo "  Services running:"
systemctl is-active redis-server postgresql asterisk 2>/dev/null | paste - - - | \
    awk 'BEGIN{split("redis-server postgresql asterisk",n," ")} {for(i=1;i<=NF;i++) printf "    %-20s %s\n", n[i], $i}'
echo ""
echo "  To start DialFlow Pro:"
echo "    chmod +x start.sh && ./start.sh"
echo ""
echo "  URLs (after start.sh):"
echo "    App        : https://127.0.0.1"
echo "    Admin      : https://127.0.0.1/admin/"
echo "    Dashboard  : https://127.0.0.1/dashboard/"
echo ""
