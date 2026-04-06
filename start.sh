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

# Start Daphne in the background
echo "[1/3] Starting Django Server (Daphne HTTPS on Port 8000)..."
daphne -e ssl:8000:privateKey=keys/key.pem:certKey=keys/cert.pem dialflow.asgi:application &
DAPHNE_PID=$!

# Start Celery worker in the background
echo "[2/3] Starting Celery Worker..."
celery -A dialflow worker -l info &
WORKER_PID=$!

# Start Celery beat in the background
echo "[3/3] Starting Celery Beat..."
celery -A dialflow beat -l info &
BEAT_PID=$!

# Handle shutdown gracefully
function cleanup() {
    echo ""
    echo "Stopping DialFlow Pro services..."
    kill $DAPHNE_PID $WORKER_PID $BEAT_PID 2>/dev/null
    wait $DAPHNE_PID $WORKER_PID $BEAT_PID 2>/dev/null
    echo "All services stopped successfully."
    exit
}

# Trap Ctrl+C (SIGINT) and termination signals
trap cleanup SIGINT SIGTERM

echo ""
echo "All DialFlow Pro components are running!"
echo "----------------------------------------"
echo "Open in browser: https://127.0.0.1:8000"
echo "Press Ctrl+C to securely stop all services."
echo "----------------------------------------"

# Keep the script running to hold the trap
wait
