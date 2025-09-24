#!/bin/bash

echo "=== Диагностика Production окружения ==="
echo ""

# Проверка процессов Gunicorn
echo "1. Процессы Gunicorn:"
ps aux | grep gunicorn | grep -v grep
echo ""

# Проверка использования памяти
echo "2. Использование памяти:"
free -h
echo ""

# Проверка открытых соединений к PostgreSQL
echo "3. Активные соединения к PostgreSQL:"
netstat -an | grep :5432 | wc -l
echo ""

# Проверка лимитов системы
echo "4. Лимиты системы для процесса:"
if [ -f /proc/$(pgrep -f "gunicorn.*master" | head -1)/limits ]; then
    cat /proc/$(pgrep -f "gunicorn.*master" | head -1)/limits | grep -E "Max open files|Max processes"
fi
echo ""

# Проверка последних ошибок в логах
echo "5. Последние ошибки Gunicorn (если есть):"
if [ -f logs/gunicorn_error.log ]; then
    tail -20 logs/gunicorn_error.log | grep -E "ERROR|CRITICAL|consuming input failed"
fi
echo ""

# Проверка состояния PostgreSQL
echo "6. Проверка подключения к PostgreSQL:"
PGPASSWORD="" psql -h 10.8.0.1 -U elcrypto -d fox_crypto_new -c "SELECT version();" 2>&1 | head -1
echo ""

# Проверка SSL соединений в PostgreSQL
echo "7. SSL соединения в PostgreSQL:"
PGPASSWORD="" psql -h 10.8.0.1 -U elcrypto -d fox_crypto_new -c "SELECT count(*), ssl FROM pg_stat_ssl GROUP BY ssl;" 2>&1
echo ""

# Проверка активных соединений в PostgreSQL
echo "8. Активные соединения в базе данных:"
PGPASSWORD="" psql -h 10.8.0.1 -U elcrypto -d fox_crypto_new -c "SELECT count(*) as connections, state FROM pg_stat_activity WHERE datname = 'fox_crypto_new' GROUP BY state;" 2>&1
echo ""

echo "=== Диагностика завершена ===