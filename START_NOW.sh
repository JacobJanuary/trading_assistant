#!/bin/bash

# Простой и надежный запуск Trading Assistant

echo "=== ЗАПУСК TRADING ASSISTANT ==="
echo ""

# 1. Переход в директорию
cd /home/elcrypto/trading_assistant

# 2. Активация окружения
echo "Активация виртуального окружения..."
source venv/bin/activate

# 3. Обновление кода
echo "Получение последних изменений..."
git pull

# 4. Остановка старых процессов
echo "Остановка старых процессов..."
pkill -f gunicorn
sleep 2

# 5. Запуск приложения используя конфигурацию
echo "Запуск Gunicorn..."
gunicorn --config gunicorn_config.py app:app &

PID=$!
echo ""
echo "✅ Gunicorn запущен с PID: $PID"

# 6. Проверка через 5 секунд
sleep 5

echo ""
echo "Проверка состояния:"
if ps -p $PID > /dev/null; then
    echo "✅ Процесс работает"
    
    # Проверка доступности
    response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/)
    if [ "$response" = "200" ] || [ "$response" = "302" ]; then
        echo "✅ Приложение отвечает (код: $response)"
        echo ""
        echo "=== ЗАПУСК УСПЕШЕН ==="
        echo "Для остановки: kill $PID"
    else
        echo "⚠️  Приложение не отвечает (код: $response)"
        echo "Проверьте логи: tail -f logs/gunicorn_error.log"
    fi
else
    echo "❌ Процесс не запустился"
    echo "Проверьте логи для диагностики:"
    echo "tail -100 logs/gunicorn_error.log"
fi