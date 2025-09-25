#!/bin/bash
# Проверка ошибки 500 в SSE endpoint

echo "=== Проверка ошибки 500 в SSE endpoint ==="
echo ""

# Проверяем последние ошибки
echo "Последние ошибки в логах Trading Assistant:"
sudo journalctl -u trading_assistant --since "5 minutes ago" | grep -A 10 -B 2 "ERROR\|Exception\|Traceback\|500"

echo ""
echo "=== Проверка импорта Celery модулей ==="
cd /home/elcrypto/trading_assistant
source venv/bin/activate

python -c "
import sys
import traceback

try:
    from config import Config
    print(f'Config.USE_CELERY = {Config.USE_CELERY}')
    
    if Config.USE_CELERY:
        try:
            from celery_sse_endpoints import analyze_efficiency_celery
            print('✓ celery_sse_endpoints.analyze_efficiency_celery импортирован')
        except ImportError as e:
            print(f'✗ Ошибка импорта celery_sse_endpoints: {e}')
            traceback.print_exc()
            
        try:
            from celery_app import celery_app
            print('✓ celery_app импортирован')
        except ImportError as e:
            print(f'✗ Ошибка импорта celery_app: {e}')
            
        try:
            from celery_tasks import analyze_efficiency_combination
            print('✓ celery_tasks импортирован')
        except ImportError as e:
            print(f'✗ Ошибка импорта celery_tasks: {e}')
    else:
        print('USE_CELERY выключен')
except Exception as e:
    print(f'Общая ошибка: {e}')
    traceback.print_exc()
"

echo ""
echo "=== Мониторинг логов в реальном времени ==="
echo "Сейчас запустите анализ в браузере..."
sudo journalctl -u trading_assistant -f --no-pager | grep -A 5 "analyze_30days_progress\|ERROR\|Exception"