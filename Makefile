# DialFlow Pro — Makefile
# Usage: make <target>

.PHONY: help install migrate seed run worker beat test shell clean docker-up docker-down

help:
	@echo ""
	@echo "  DialFlow Pro — Development Commands"
	@echo "  ─────────────────────────────────────"
	@echo "  make install      Install Python dependencies"
	@echo "  make migrate      Run database migrations"
	@echo "  make seed         Seed initial data (dispositions, server, campaign)"
	@echo "  make run          Start Django dev server (ARI worker auto-starts)"
	@echo "  make worker       Start Celery worker"
	@echo "  make beat         Start Celery beat scheduler"
	@echo "  make dev          Start all 3 services (requires tmux)"
	@echo "  make test         Run test suite"
	@echo "  make shell        Django shell"
	@echo "  make clean        Remove pyc files and logs"
	@echo "  make docker-up    Start Docker Compose stack"
	@echo "  make docker-down  Stop Docker Compose stack"
	@echo ""

install:
	pip install -r requirements.txt

migrate:
	python manage.py migrate

superuser:
	python manage.py createsuperuser

seed: migrate
	python manage.py setup_initial_data

run:
	python manage.py runserver 0.0.0.0:8000

worker:
	celery -A dialflow worker -l info --concurrency=4

beat:
	celery -A dialflow beat -l info

# Requires tmux
dev:
	@tmux new-session -d -s dialflow -n web 'make run' \; \
	       new-window -t dialflow -n worker 'make worker' \; \
	       new-window -t dialflow -n beat 'make beat' \; \
	       attach-session -t dialflow
	@echo "Started dialflow tmux session. Ctrl+b d to detach."

test:
	pytest tests/ -v

test-fast:
	pytest tests/ -x -q

shell:
	python manage.py shell_plus 2>/dev/null || python manage.py shell

static:
	python manage.py collectstatic --no-input

clean:
	find . -type f -name "*.pyc" -delete
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	rm -f logs/*.log

docker-up:
	docker-compose up -d
	@echo "Waiting for services..."
	@sleep 5
	docker-compose exec web python manage.py migrate
	docker-compose exec web python manage.py setup_initial_data
	@echo ""
	@echo "  DialFlow Pro running at http://localhost:8000"
	@echo "  Create admin: docker-compose exec web python manage.py createsuperuser"

docker-down:
	docker-compose down

docker-logs:
	docker-compose logs -f --tail=100

reset-db:
	@echo "WARNING: This will drop and recreate the database!"
	@read -p "Are you sure? (yes/no): " confirm && [ "$$confirm" = "yes" ]
	python manage.py flush --no-input
	python manage.py migrate
	python manage.py setup_initial_data
