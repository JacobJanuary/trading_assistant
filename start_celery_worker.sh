#!/bin/bash
# Запуск Celery воркера

echo "Starting Celery worker..."

# Активируем виртуальное окружение
source venv/bin/activate

# Запускаем Celery воркер
celery -A celery_app worker \
    --loglevel=info \
    --concurrency=2 \
    --max-tasks-per-child=50 \
    --time-limit=3600 \
    --soft-time-limit=3300 \
    -n worker1@%h