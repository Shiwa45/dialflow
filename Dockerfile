FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# System deps: psycopg2, Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc libffi-dev libssl-dev libjpeg-dev libpng-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN python manage.py collectstatic --no-input 2>/dev/null || true

EXPOSE 8000

CMD ["daphne", "-b", "0.0.0.0", "-p", "8000", "dialflow.asgi:application"]
