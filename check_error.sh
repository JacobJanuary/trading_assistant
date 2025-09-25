#!/bin/bash
# Проверка ошибки 500 в SSE endpoint

echo "=== Проверка ошибки 500 в SSE endpoint ==="
echo ""

# Последние ошибки в приложении
echo "Последние ошибки в логах приложения:"
sudo journalctl -u trading_assistant -n 100 | grep -A 5 -B 2 "analyze_30days_progress\|500\|Internal Server Error\|Exception\|Traceback"

echo ""
echo "=== Проверка импорта модулей в приложении ==="
python -c "
import sys
sys.path.insert(0, '.')
try:
    from config import Config
    print(f'Config.USE_CELERY = {Config.USE_CELERY}')
    
    if Config.USE_CELERY:
        from celery_sse_endpoints import analyze_efficiency_celery
        print('✓ celery_sse_endpoints импортирован')
        
        from celery_app import celery_app
        print('✓ celery_app импортирован')
        
        from celery_tasks import analyze_efficiency_combination
        print('✓ celery_tasks импортирован')
    else:
        print('USE_CELERY = False, Celery модули не используются')
except Exception as e:
    print(f'✗ Ошибка: {e}')
    import traceback
    traceback.print_exc()
"

echo ""
echo "=== Тест SSE endpoint напрямую ==="
echo "Пытаемся вызвать endpoint..."

# Получаем cookie сессии
SESSION_COOKIE=$(curl -s -c - http://10.8.0.1:7777/login | grep session | awk '{print $7}')

if [ -z "$SESSION_COOKIE" ]; then
    echo "Не удалось получить cookie сессии. Проверьте, что приложение запущено."
else
    echo "Отправляем запрос к SSE endpoint..."
    curl -v -H "Cookie: session=$SESSION_COOKIE" \
        "http://10.8.0.1:7777/api/efficiency/analyze_30days_progress?score_week_min=60&score_week_max=80&score_month_min=60&score_month_max=80&step=10&max_trades_per_15min=3" \
        2>&1 | head -50
fi