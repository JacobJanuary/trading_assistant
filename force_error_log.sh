#!/bin/bash
# Принудительная проверка ошибки 500

echo "=== Принудительная диагностика ошибки 500 ==="
echo ""

# 1. Перезапускаем приложение чтобы точно применились изменения
echo "1. Перезапуск приложения..."
sudo systemctl restart trading_assistant
sleep 3

# 2. Проверяем что приложение запустилось
echo "2. Статус приложения:"
sudo systemctl status trading_assistant | head -15

# 3. Проверяем логи запуска на ошибки
echo ""
echo "3. Ошибки при запуске:"
sudo journalctl -u trading_assistant --since "1 minute ago" | grep -E "ERROR|Exception|Failed|ImportError" | head -10

# 4. Делаем прямой запрос к endpoint
echo ""
echo "4. Прямой тест SSE endpoint с curl:"

# Сначала получаем cookie
curl -s -c /tmp/cookies.txt -X POST http://10.8.0.1:7777/login \
  -d "username=RichMan&password=LohNeMamont@!21" > /dev/null 2>&1

# Делаем запрос и смотрим первые строки ответа
echo "Ответ сервера:"
curl -s -b /tmp/cookies.txt \
  "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true&score_week_min=60&score_week_max=80&score_month_min=60&score_month_max=80&step=10&max_trades_per_15min=3" \
  2>&1 | head -20

# 5. Проверяем все логи gunicorn
echo ""
echo "5. Все последние логи gunicorn:"
sudo journalctl -u trading_assistant --since "30 seconds ago" --no-pager

# 6. Проверяем, может ошибка в nginx логах
echo ""
echo "6. Проверка nginx error.log:"
sudo tail -20 /var/log/nginx/error.log | grep -E "7777|trading"

# 7. Альтернативный способ - смотрим прямо в gunicorn stderr
echo ""
echo "7. Проверка gunicorn stderr:"
sudo journalctl -u trading_assistant -o cat --since "1 minute ago" | grep -A 5 -B 5 "500\|Internal Server Error"