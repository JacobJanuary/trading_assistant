#!/bin/bash

echo "=== Полная очистка и перезапуск сервисов ==="
echo ""

# 1. Останавливаем сервисы
echo "1. Останавливаем сервисы..."
sudo systemctl stop celery_efficiency
sudo systemctl stop trading_assistant
sleep 2

# 2. Убиваем зависшие процессы Celery
echo "2. Убиваем зависшие процессы..."
sudo pkill -9 -f "celery.*efficiency" 2>/dev/null || true
sleep 1

# 3. Очищаем Redis полностью
echo "3. Очищаем Redis..."
redis-cli FLUSHALL > /dev/null
echo "   Redis очищен"

# 4. Очищаем задачи в БД
echo "4. Очищаем задачи в БД..."
PGPASSWORD='LohNeMamont@!21' psql -U elcrypto -d fox_crypto_new -h 10.8.0.1 -c "DELETE FROM web.analysis_tasks WHERE task_type='efficiency_analysis';" > /dev/null 2>&1
echo "   База данных очищена"

# 5. Запускаем сервисы в правильном порядке
echo "5. Запускаем сервисы..."
echo "   - Запуск Flask..."
sudo systemctl start trading_assistant
sleep 2

echo "   - Запуск Celery..."
sudo systemctl start celery_efficiency
sleep 2

# 6. Проверяем статус
echo ""
echo "=== Статус сервисов ==="
echo ""
echo "Flask:"
sudo systemctl is-active trading_assistant

echo ""
echo "Celery:"
sudo systemctl is-active celery_efficiency

echo ""
echo "Процессы Celery:"
ps aux | grep celery | grep -v grep | wc -l

echo ""
echo "Redis:"
redis-cli DBSIZE

echo ""
echo "✅ Перезапуск завершен!"
echo ""
echo "Проверьте веб-интерфейс: http://10.8.0.1:7777/efficiency_analysis"