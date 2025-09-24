#!/bin/bash

# Скрипт запуска приложения в production режиме с исправлениями SSL

# Активация виртуального окружения если есть
if [ -d "venv" ]; then
    source venv/bin/activate
elif [ -d ".venv" ]; then
    source .venv/bin/activate
fi

# Загрузка production окружения
if [ -f ".env.production" ]; then
    export $(grep -v '^#' .env.production | xargs)
    echo "Загружены production настройки из .env.production"
elif [ -f ".env" ]; then
    export $(grep -v '^#' .env | xargs)
    echo "Загружены настройки из .env"
fi

# Экспорт переменных окружения
export FLASK_APP=app.py
export FLASK_ENV=production

# Увеличиваем лимиты системы для процесса
ulimit -n 4096  # Увеличиваем лимит открытых файлов
ulimit -u 2048  # Увеличиваем лимит процессов

# Проверка .pgpass файла
if [ ! -f "$HOME/.pgpass" ]; then
    echo "ВНИМАНИЕ: Файл .pgpass не найден в $HOME/.pgpass"
    echo "Создайте файл со следующим форматом:"
    echo "hostname:port:database:username:password"
    exit 1
fi

# Проверка прав доступа .pgpass
PGPASS_PERMS=$(stat -c %a "$HOME/.pgpass" 2>/dev/null || stat -f %A "$HOME/.pgpass" 2>/dev/null)
if [ "$PGPASS_PERMS" != "600" ]; then
    echo "Исправляем права доступа .pgpass (должно быть 600)"
    chmod 600 "$HOME/.pgpass"
fi

# Определение временной директории для воркеров
if [ -d "/dev/shm" ]; then
    WORKER_TMP="--worker-tmp-dir /dev/shm"
    echo "Используется /dev/shm для временных файлов воркеров"
else
    WORKER_TMP=""
    echo "Примечание: /dev/shm недоступен, используется стандартная временная директория"
fi

# Создание директории для логов если не существует
mkdir -p logs

# Остановка старых процессов Gunicorn если есть
echo "Проверка старых процессов Gunicorn..."
if pgrep -f "gunicorn.*app:app" > /dev/null; then
    echo "Остановка старых процессов Gunicorn..."
    pkill -f "gunicorn.*app:app"
    sleep 2
fi

# Запуск Gunicorn с конфигурацией
echo "Запуск Gunicorn с production конфигурацией..."
echo "  - Воркеры: максимум 4"
echo "  - Таймаут: 5 минут для длительных операций"
echo "  - Keep-alive: 75 секунд для SSE соединений"
echo "  - SSL режим: prefer (автоматический выбор)"
echo ""

# Запуск с логированием
gunicorn \
    --config gunicorn_config.py \
    $WORKER_TMP \
    --access-logfile logs/gunicorn_access.log \
    --error-logfile logs/gunicorn_error.log \
    --log-level info \
    --capture-output \
    app:app

# Если Gunicorn завершился с ошибкой
if [ $? -ne 0 ]; then
    echo ""
    echo "ОШИБКА: Gunicorn завершился с ошибкой"
    echo "Проверьте логи в logs/gunicorn_error.log"
    if [ -f logs/gunicorn_error.log ]; then
        echo ""
        echo "Последние ошибки:"
        tail -20 logs/gunicorn_error.log | grep -E "ERROR|CRITICAL"
    fi
    exit 1
fi