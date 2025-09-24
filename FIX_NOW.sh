#!/bin/bash

# Скрипт экстренного восстановления Trading Assistant

echo "=== ЭКСТРЕННОЕ ВОССТАНОВЛЕНИЕ TRADING ASSISTANT ==="
echo "Время: $(date)"
echo ""

# 1. Останавливаем все процессы Gunicorn
echo "1. Останавливаем старые процессы..."
pkill -9 -f gunicorn
sleep 2

# 2. Проверяем базу данных
echo "2. Проверяем подключение к БД..."
# Используем .pgpass, не устанавливаем PGPASSWORD
psql -h 10.8.0.1 -U elcrypto -d fox_crypto_new -c "SELECT 1" > /dev/null 2>&1
if [ $? -eq 0 ]; then
    echo "   ✅ База данных доступна"
else
    echo "   ❌ Проблема с базой данных!"
    echo "   Проверьте .pgpass файл:"
    echo "   cat ~/.pgpass"
    echo "   Должна быть строка: 10.8.0.1:5432:fox_crypto_new:elcrypto:ваш_пароль"
fi

# 3. Активируем виртуальное окружение
echo "3. Активация виртуального окружения..."
cd /home/elcrypto/trading_assistant
source venv/bin/activate

# 4. Обновляем код из git
echo "4. Получаем последние исправления..."
git pull

# 5. Запускаем Gunicorn
echo "5. Запуск Gunicorn..."
echo ""

# Запуск с минимальной конфигурацией для отладки
# Проверяем версию gunicorn для совместимости
GUNICORN_VERSION=$(gunicorn --version 2>&1 | cut -d' ' -f2)
echo "   Версия Gunicorn: $GUNICORN_VERSION"

# Базовые параметры, поддерживаемые всеми версиями
gunicorn \
    --bind 0.0.0.0:5000 \
    --workers 2 \
    --timeout 300 \
    --access-logfile logs/gunicorn_access.log \
    --error-logfile logs/gunicorn_error.log \
    --log-level info \
    --capture-output \
    app:app &

GUNICORN_PID=$!
echo "Gunicorn запущен с PID: $GUNICORN_PID"

# 6. Ждем запуска
sleep 5

# 7. Проверяем что запустилось
echo ""
echo "6. Проверка состояния:"
ps aux | grep gunicorn | grep -v grep | wc -l
echo "   Запущено процессов Gunicorn: $(ps aux | grep gunicorn | grep -v grep | wc -l)"

# 8. Проверяем доступность
echo ""
echo "7. Проверка доступности приложения:"
response=$(curl -s -o /dev/null -w "%{http_code}" http://localhost:5000/ --max-time 5)
if [ "$response" = "200" ] || [ "$response" = "302" ]; then
    echo "   ✅ Приложение доступно (код: $response)"
    echo ""
    echo "=== ВОССТАНОВЛЕНИЕ УСПЕШНО ==="
else
    echo "   ❌ Приложение не отвечает (код: $response)"
    echo ""
    echo "Проверьте логи:"
    echo "tail -f logs/gunicorn_error.log"
fi

echo ""
echo "PID главного процесса: $GUNICORN_PID"
echo "Для остановки используйте: kill $GUNICORN_PID"