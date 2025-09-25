#!/bin/bash
# Отладка реальной проблемы

echo "=== Отладка реальной проблемы SSE ==="
echo ""

# 1. Проверяем что файлы действительно обновлены
echo "1. Проверка обновлений в файлах:"
echo "app.py - передача user_id:"
grep -n "analyze_efficiency_celery(current_user.id)" app.py || echo "НЕ НАЙДЕНО!"

echo ""
echo "celery_sse_endpoints.py - функция с user_id:"
grep -n "def analyze_efficiency_celery(user_id):" celery_sse_endpoints.py || echo "НЕ НАЙДЕНО!"

# 2. Перезапускаем и проверяем логи запуска
echo ""
echo "2. Перезапуск приложения..."
sudo systemctl restart trading_assistant
sleep 3

# 3. Делаем тестовый запрос и смотрим ВСЕ логи
echo ""
echo "3. Тестовый запрос с полным логированием..."

# Получаем cookie
curl -s -c /tmp/cookies.txt -X POST http://10.8.0.1:7777/login \
  -d "username=RichMan&password=LohNeMamont@!21" > /dev/null 2>&1

# Делаем запрос в фоне
(curl -s -b /tmp/cookies.txt \
  "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?use_celery=true&score_week_min=60&score_week_max=80&score_month_min=60&score_month_max=80&step=10&max_trades_per_15min=3" \
  > /tmp/sse_response.txt 2>&1) &

CURL_PID=$!

# Ждем 2 секунды
sleep 2

# Убиваем curl
kill $CURL_PID 2>/dev/null

# 4. Показываем ответ
echo "4. Ответ сервера (первые 50 строк):"
head -50 /tmp/sse_response.txt

# 5. Показываем ВСЕ логи за последние 30 секунд
echo ""
echo "5. ВСЕ логи приложения за последние 30 секунд:"
sudo journalctl -u trading_assistant --since "30 seconds ago" --no-pager

# 6. Проверяем gunicorn stderr напрямую
echo ""
echo "6. Прямая проверка stderr:"
sudo journalctl -u trading_assistant -o cat --since "1 minute ago" 2>&1 | grep -i "error\|exception\|traceback\|500" | head -20

# 7. Проверяем что Celery вообще используется
echo ""
echo "7. Проверка USE_CELERY в runtime:"
cd /home/elcrypto/trading_assistant
source venv/bin/activate
python -c "
from config import Config
print(f'USE_CELERY = {Config.USE_CELERY}')
if Config.USE_CELERY:
    try:
        from app import app
        with app.app_context():
            from celery_sse_endpoints import analyze_efficiency_celery
            print('analyze_efficiency_celery импортируется')
            # Проверяем сигнатуру функции
            import inspect
            sig = inspect.signature(analyze_efficiency_celery)
            print(f'Сигнатура: {sig}')
    except Exception as e:
        print(f'Ошибка: {e}')
        import traceback
        traceback.print_exc()
"