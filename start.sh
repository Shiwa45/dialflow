#!/bin/bash

# Ensure we're in the project directory
cd "$(dirname "$0")"

echo "==============================="
echo "   Starting DialFlow Pro       "
echo "==============================="

echo "[0/3] Checking required services (Redis & Asterisk)..."

# Check Redis
if ! pgrep -x "redis-server" > /dev/null; then
    echo "  ✖  Redis is NOT running!"
    echo "     Please run: sudo apt install -y redis-server && sudo systemctl enable --now redis-server"
    echo "     Then re-run this script."
    exit 1
else
    echo "  ✔  Redis is running."
fi

# Check Asterisk
if ! pgrep -x "asterisk" > /dev/null; then
    echo "  ✖  Asterisk is NOT running!"
    echo "     Please run: sudo systemctl start asterisk"
    echo "     Then re-run this script."
    exit 1
else
    echo "  ✔  Asterisk is running."
fi

echo "Activating virtual environment..."
source venv/bin/activate
mkdir -p logs

# Cleanup old processes if any
echo "Cleaning up any stale services..."
sudo pkill -f "daphne" 2>/dev/null
sudo pkill -f "run_ari" 2>/dev/null
sudo pkill -f "celery" 2>/dev/null

# Start Daphne in the background
DAPHNE_SSL_PORT="443"
echo "[1/4] Starting Django Server (Daphne HTTPS on Port ${DAPHNE_SSL_PORT})..."
daphne -e ssl:${DAPHNE_SSL_PORT}:interface=0.0.0.0:privateKey=keys/key.pem:certKey=keys/cert.pem dialflow.asgi:application >> logs/daphne.out 2>&1 &
DAPHNE_PID=$!

# Start Standalone ARI Worker
echo "[2/4] Starting ARI Worker Process..."
python manage.py run_ari >> logs/ari_worker.log 2>&1 &
ARI_PID=$!

# Start Celery worker in the background
echo "[3/4] Starting Celery Worker..."
celery -A dialflow worker -l info >> logs/celery_worker.out 2>&1 &
WORKER_PID=$!

# Start Celery beat in the background
echo "[4/4] Starting Celery Beat..."
celery -A dialflow beat -l info >> logs/celery_beat.out 2>&1 &
BEAT_PID=$!

# Handle shutdown gracefully
function cleanup() {
    echo ""
    echo "Stopping DialFlow Pro services..."
    kill $DAPHNE_PID $ARI_PID $WORKER_PID $BEAT_PID 2>/dev/null
    wait $DAPHNE_PID $ARI_PID $WORKER_PID $BEAT_PID 2>/dev/null
    echo "All services stopped successfully."
    exit
}

# Trap Ctrl+C (SIGINT) and termination signals
trap cleanup SIGINT SIGTERM

echo ""
echo "All DialFlow Pro components are running!"
echo "----------------------------------------"
if [ "$DAPHNE_SSL_PORT" = "443" ]; then
    echo "Open in browser: https://127.0.0.1"
else
    echo "Open in browser: https://127.0.0.1:${DAPHNE_SSL_PORT}"
fi
echo "Key logs:"
echo "  logs/daphne.out"
echo "  logs/celery_worker.out"
echo "  logs/celery_beat.out"
echo "  logs/dialer.log"
echo "  logs/ari_worker.log"
echo "Press Ctrl+C to securely stop all services."
echo "----------------------------------------"

# Keep the script running to hold the trap
wait
