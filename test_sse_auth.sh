#!/bin/bash
# Тест SSE с правильной авторизацией

echo "=== Тест SSE с авторизацией ==="
echo ""

# Замените на ваши реальные учетные данные
USERNAME="RichMan"  # <-- ЗАМЕНИТЕ НА ВАШ ЛОГИН
PASSWORD="LohNeMamont@!21"  # <-- ЗАМЕНИТЕ НА ВАШ ПАРОЛЬ

echo "1. Логин в систему..."
# Логинимся и сохраняем все cookies
RESPONSE=$(curl -s -c cookies.txt -L -X POST http://10.8.0.1:7777/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=${USERNAME}&password=${PASSWORD}" \
  -w "\nHTTP_CODE:%{http_code}")

HTTP_CODE=$(echo "$RESPONSE" | grep "HTTP_CODE:" | cut -d: -f2)

if [ "$HTTP_CODE" = "200" ] || [ "$HTTP_CODE" = "302" ]; then
    echo "✓ Логин успешен (HTTP $HTTP_CODE)"
    
    # Проверяем cookie
    if grep -q "session" cookies.txt; then
        echo "✓ Session cookie получен:"
        grep "session" cookies.txt | awk '{print "  " $6 "=" substr($7, 1, 20) "..."}'
    else
        echo "✗ Session cookie не найден"
    fi
else
    echo "✗ Логин неудачен (HTTP $HTTP_CODE)"
    echo "Проверьте логин и пароль в скрипте"
    exit 1
fi

echo ""
echo "2. Тест SSE endpoint с авторизацией:"
echo "URL: http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true"
echo ""

# Делаем запрос с cookie
curl -N -b cookies.txt \
  -H "Accept: text/event-stream" \
  -H "Cache-Control: no-cache" \
  "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true&score_week_min=60&score_week_max=80&score_month_min=60&score_month_max=80&step=10&max_trades_per_15min=3&session_id=test_$(date +%s)" \
  2>/dev/null | while IFS= read -r line; do
    # Показываем первые 10 событий
    echo "$line"
    if [[ "$line" == *"complete"* ]]; then
        echo ""
        echo "✓ SSE работает корректно!"
        break
    fi
done &

# Даем время на получение нескольких событий
sleep 5
pkill -f "curl.*analyze_30days_progress" 2>/dev/null

echo ""
echo "3. Проверка заголовков ответа:"
curl -I -b cookies.txt \
  "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true" \
  2>/dev/null | grep -E "HTTP|Content-Type|Location"

echo ""
echo "Если видите:"
echo "  - 'HTTP/1.1 200 OK' и 'Content-Type: text/event-stream' - всё работает"
echo "  - 'HTTP/1.1 302' - проблема с авторизацией"  
echo "  - 'HTTP/1.1 500' - ошибка сервера (смотрите логи)"