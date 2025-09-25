#!/bin/bash
# Диагностика проблем с Celery и SSE

echo "=== Диагностика Celery и SSE ==="
echo ""

# 1. Проверяем USE_CELERY в .env
echo "1. Проверка конфигурации:"
grep "^USE_CELERY=" .env
echo ""

# 2. Проверяем статус служб
echo "2. Статус служб:"
echo -n "  Redis: "
systemctl is-active redis-server || echo "не активен"
echo -n "  Celery: "
systemctl is-active celery-worker || echo "не активен"
echo -n "  Trading Assistant: "
systemctl is-active trading_assistant || echo "не активен"
echo ""

# 3. Проверяем, что Celery видит задачи
echo "3. Зарегистрированные Celery задачи:"
python -c "
from celery_app import celery_app
tasks = [t for t in celery_app.tasks.keys() if 'analyze' in t]
for task in tasks:
    print(f'  - {task}')
" 2>/dev/null || echo "  Ошибка при проверке задач"
echo ""

# 4. Проверяем последние логи приложения
echo "4. Последние ошибки в логах приложения:"
sudo journalctl -u trading_assistant -n 50 | grep -i "error\|exception\|celery" | tail -10
echo ""

# 5. Проверяем Celery worker логи
echo "5. Последние логи Celery worker:"
sudo journalctl -u celery-worker -n 20 --no-pager | grep -v "INFO"
echo ""

# 6. Проверяем, что приложение видит Celery конфигурацию
echo "6. Проверка конфигурации в Python:"
python -c "
from config import Config
print(f'  USE_CELERY = {Config.USE_CELERY}')
print(f'  CELERY_BROKER_URL = {Config.CELERY_BROKER_URL}')
print(f'  Redis доступен: ', end='')
import redis
try:
    r = redis.Redis(host='localhost', port=6379, db=0)
    r.ping()
    print('Да')
except:
    print('Нет')
" 2>/dev/null || echo "  Ошибка при проверке конфигурации"
echo ""

# 7. Проверяем, что приложение может импортировать Celery endpoints
echo "7. Проверка импортов:"
python -c "
try:
    from celery_sse_endpoints import analyze_efficiency_celery
    print('  ✓ celery_sse_endpoints импортируется')
except Exception as e:
    print(f'  ✗ Ошибка импорта: {e}')
" 2>/dev/null
echo ""

echo "=== Диагностика завершена ==="
echo ""
echo "Если все проверки пройдены, но SSE все равно не работает:"
echo "1. Перезапустите приложение: sudo systemctl restart trading_assistant"
echo "2. Проверьте браузерную консоль (F12) на наличие ошибок"
echo "3. Попробуйте открыть приложение в режиме инкогнито"