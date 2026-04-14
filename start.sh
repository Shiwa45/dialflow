#!/bin/bash

# Ensure we're in the project directory
cd "$(dirname "$0")"

echo "==============================="
echo "   Starting DialFlow Pro       "
echo "==============================="

# ── Service Checks ────────────────────────────────────────────────────────────
echo "[0/4] Checking required services (Redis & Asterisk)..."

# Check Redis (native Ubuntu: use systemctl)
if ! systemctl is-active --quiet redis-server 2>/dev/null; then
    echo "  ✖  Redis is NOT running!"
    echo "     Starting Redis automatically..."
    sudo systemctl start redis-server
    sleep 1
    if ! systemctl is-active --quiet redis-server 2>/dev/null; then
        echo "  ✖  Failed to start Redis. Run: sudo systemctl enable --now redis-server"
        exit 1
    fi
fi
echo "  ✔  Redis is running."

# Check Asterisk
if ! systemctl is-active --quiet asterisk 2>/dev/null; then
    echo "  ✖  Asterisk is NOT running!"
    echo "     Starting Asterisk automatically..."
    sudo systemctl start asterisk
    sleep 2
    if ! systemctl is-active --quiet asterisk 2>/dev/null; then
        echo "  ✖  Failed to start Asterisk. Run: sudo systemctl enable --now asterisk"
        exit 1
    fi
fi
echo "  ✔  Asterisk is running."

# Check PostgreSQL
if ! systemctl is-active --quiet postgresql 2>/dev/null; then
    echo "  ✖  PostgreSQL is NOT running!"
    echo "     Starting PostgreSQL automatically..."
    sudo systemctl start postgresql
    sleep 2
fi
echo "  ✔  PostgreSQL is running."

# ── Virtual Environment ────────────────────────────────────────────────────────
echo ""
echo "Activating virtual environment..."
source venv/bin/activate

mkdir -p logs

# ── Cleanup stale processes ────────────────────────────────────────────────────
echo "Cleaning up any stale services..."
pkill -f "daphne" 2>/dev/null
pkill -f "run_ari" 2>/dev/null
pkill -f "celery" 2>/dev/null
sleep 1

# ── SSL Mode vs HTTP Mode ──────────────────────────────────────────────────────
# Detect whether to use HTTPS (port 443) or HTTP (port 8000)
USE_SSL=true
SSL_PORT=443
HTTP_PORT=8000

if [ ! -f "keys/cert.pem" ] || [ ! -f "keys/key.pem" ]; then
    echo "  ⚠  SSL certs not found — falling back to HTTP mode (port ${HTTP_PORT})"
    USE_SSL=false
fi

# ── Start Daphne ──────────────────────────────────────────────────────────────
if [ "$USE_SSL" = true ]; then
    echo "[1/4] Starting Django Server (Daphne HTTPS on port ${SSL_PORT})..."
    # Use sudo only if binding to privileged port 443
    if [ "$SSL_PORT" -lt 1024 ]; then
        sudo venv/bin/daphne \
            -e ssl:${SSL_PORT}:interface=0.0.0.0:privateKey=keys/key.pem:certKey=keys/cert.pem \
            dialflow.asgi:application >> logs/daphne.out 2>&1 &
    else
        daphne \
            -e ssl:${SSL_PORT}:interface=0.0.0.0:privateKey=keys/key.pem:certKey=keys/cert.pem \
            dialflow.asgi:application >> logs/daphne.out 2>&1 &
    fi
else
    echo "[1/4] Starting Django Server (Daphne HTTP on port ${HTTP_PORT})..."
    daphne -b 0.0.0.0 -p ${HTTP_PORT} dialflow.asgi:application >> logs/daphne.out 2>&1 &
fi
DAPHNE_PID=$!

# ── Start ARI Worker ──────────────────────────────────────────────────────────
echo "[2/4] Starting ARI Worker Process..."
python manage.py run_ari >> logs/ari_worker.log 2>&1 &
ARI_PID=$!

# ── Start Celery Worker ───────────────────────────────────────────────────────
echo "[3/4] Starting Celery Worker..."
celery -A dialflow worker -l info >> logs/celery_worker.out 2>&1 &
WORKER_PID=$!

# ── Start Celery Beat ─────────────────────────────────────────────────────────
echo "[4/4] Starting Celery Beat..."
celery -A dialflow beat -l info >> logs/celery_beat.out 2>&1 &
BEAT_PID=$!

# ── Graceful Shutdown ─────────────────────────────────────────────────────────
function cleanup() {
    echo ""
    echo "Stopping DialFlow Pro services..."
    kill $DAPHNE_PID $ARI_PID $WORKER_PID $BEAT_PID 2>/dev/null
    # Also kill any sudo-spawned daphne
    sudo pkill -f "daphne" 2>/dev/null
    wait $DAPHNE_PID $ARI_PID $WORKER_PID $BEAT_PID 2>/dev/null
    echo "All services stopped successfully."
    exit 0
}

trap cleanup SIGINT SIGTERM

echo ""
echo "All DialFlow Pro components are running!"
echo "----------------------------------------"
if [ "$USE_SSL" = true ]; then
    if [ "$SSL_PORT" = "443" ]; then
        echo "  Open in browser : https://127.0.0.1"
    else
        echo "  Open in browser : https://127.0.0.1:${SSL_PORT}"
    fi
else
    echo "  Open in browser : http://127.0.0.1:${HTTP_PORT}"
fi
echo ""
echo "  Key logs:"
echo "    logs/daphne.out         (web server)"
echo "    logs/celery_worker.out  (task worker)"
echo "    logs/celery_beat.out    (scheduler)"
echo "    logs/ari_worker.log     (Asterisk ARI)"
echo "    logs/dialflow.log       (app log)"
echo ""
echo "Press Ctrl+C to securely stop all services."
echo "----------------------------------------"

wait
