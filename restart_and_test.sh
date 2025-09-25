#!/bin/bash
# Перезапуск приложения и проверка Celery

echo "=== Перезапуск Trading Assistant с Celery ==="
echo ""

# 1. Проверяем конфигурацию
echo "1. Текущая конфигурация:"
grep "^USE_CELERY=" .env
echo ""

# 2. Перезапускаем приложение
echo "2. Перезапуск приложения..."
sudo systemctl restart trading_assistant
sleep 3

# 3. Проверяем статус
echo "3. Статус служб:"
echo -n "  Trading Assistant: "
systemctl is-active trading_assistant
echo -n "  Celery Worker: "
systemctl is-active celery-worker
echo -n "  Redis: "
systemctl is-active redis-server
echo ""

# 4. Проверяем логи на ошибки
echo "4. Проверка ошибок запуска:"
sudo journalctl -u trading_assistant -n 20 | grep -i "error\|exception\|failed" | head -5 || echo "  Ошибок не найдено"
echo ""

# 5. Проверяем Celery через API
echo "5. Проверка Celery через API..."
sleep 2

# Проверяем без авторизации (может не работать)
STATUS=$(curl -s http://10.8.0.1:7777/api/config/celery_status 2>/dev/null)

if [[ "$STATUS" == *"USE_CELERY"* ]]; then
    echo "API отвечает:"
    echo "$STATUS" | python -m json.tool 2>/dev/null || echo "$STATUS"
else
    echo "API требует авторизации или недоступен"
fi

echo ""
echo "6. Мониторинг логов в реальном времени:"
echo "   Откройте браузер и запустите анализ эффективности"
echo "   Логи будут отображаться ниже:"
echo ""
sudo journalctl -u trading_assistant -f | grep --line-buffered -E "Efficiency analysis|USE_CELERY|Using.*version|SSE|Celery"