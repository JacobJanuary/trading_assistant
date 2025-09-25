#!/bin/bash
# Принудительная перезагрузка с очисткой кеша Python

echo "=== Принудительная перезагрузка приложения ==="
echo ""

# 1. Останавливаем приложение
echo "1. Остановка приложения..."
sudo systemctl stop trading_assistant

# 2. Очищаем Python кеш
echo "2. Очистка Python кеша..."
find /home/elcrypto/trading_assistant -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null
find /home/elcrypto/trading_assistant -name "*.pyc" -delete 2>/dev/null

# 3. Проверяем что изменения есть в файлах
echo "3. Проверка изменений:"
echo -n "  analyze_efficiency_celery(user_id): "
grep -q "def analyze_efficiency_celery(user_id):" celery_sse_endpoints.py && echo "✓" || echo "✗"

echo -n "  analyze_efficiency_celery(current_user.id): "
grep -q "analyze_efficiency_celery(current_user.id)" app.py && echo "✓" || echo "✗"

# 4. Перезапускаем
echo "4. Запуск приложения..."
sudo systemctl start trading_assistant
sleep 3

# 5. Проверяем статус
echo "5. Статус:"
sudo systemctl is-active trading_assistant

# 6. Мониторим логи
echo ""
echo "6. Мониторинг логов (запустите анализ в браузере):"
sudo journalctl -u trading_assistant -f --lines=0