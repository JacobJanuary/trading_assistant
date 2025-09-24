#!/bin/bash

# Скрипт запуска приложения в production режиме с Gunicorn

# Активация виртуального окружения если есть
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Экспорт переменных окружения
export FLASK_APP=app.py
export FLASK_ENV=production

# Увеличиваем лимиты системы для процесса
ulimit -n 4096  # Увеличиваем лимит открытых файлов
ulimit -u 2048  # Увеличиваем лимит процессов

# Определение временной директории для воркеров
if [ -d "/dev/shm" ]; then
    WORKER_TMP="--worker-tmp-dir /dev/shm"
    echo "Using /dev/shm for worker temporary files"
else
    WORKER_TMP=""
    echo "Note: /dev/shm not available, using default worker tmp dir"
fi

# Запуск Gunicorn с конфигурацией
echo "Starting Gunicorn with production configuration..."
echo "Workers will have 5 minute timeout for long operations"
echo "Keep-alive: 75 seconds for SSE connections"

gunicorn \
    --config gunicorn_config.py \
    $WORKER_TMP \
    app:app