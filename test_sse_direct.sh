#!/bin/bash
# Прямой тест SSE endpoint

echo "=== Прямой тест SSE endpoint ==="
echo ""

# Тест без авторизации
echo "1. Тест без авторизации:"
curl -v -H "Accept: text/event-stream" \
  "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true" \
  2>&1 | head -30

echo ""
echo "2. Получение cookie сессии:"
# Логинимся и сохраняем cookie
curl -c cookies.txt -X POST http://10.8.0.1:7777/login \
  -d "username=admin&password=admin" \
  2>/dev/null | grep -E "Redirecting|Location" || echo "Логин не удался, проверьте учетные данные"

if [ -f cookies.txt ]; then
    echo "Cookie получен"
    echo ""
    echo "3. Тест с авторизацией:"
    curl -v -b cookies.txt \
      -H "Accept: text/event-stream" \
      "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true&score_week_min=60&score_week_max=80&score_month_min=60&score_month_max=80&step=10&max_trades_per_15min=3&session_id=test_123" \
      2>&1 | head -50
else
    echo "Не удалось получить cookie"
fi

echo ""
echo "4. Проверка Content-Type в ответе:"
echo "Если видите 'Content-Type: text/html' вместо 'Content-Type: text/event-stream',"
echo "то сервер возвращает HTML страницу ошибки вместо SSE потока."