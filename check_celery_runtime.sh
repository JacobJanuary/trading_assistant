#!/bin/bash
# Проверка USE_CELERY в runtime

echo "=== Проверка USE_CELERY в работающем приложении ==="
echo ""

# 1. Проверяем .env
echo "1. Значение в .env:"
grep "^USE_CELERY=" .env
echo ""

# 2. Проверяем, какой .env используется
echo "2. Проверка всех .env файлов:"
ls -la .env*
echo ""

# 3. Проверяем процесс gunicorn
echo "3. Проверка переменных окружения gunicorn:"
sudo cat /proc/$(pgrep -f "gunicorn.*trading_assistant" | head -1)/environ 2>/dev/null | tr '\0' '\n' | grep -E "USE_CELERY|CELERY" || echo "Не удалось получить переменные окружения"
echo ""

# 4. Делаем тестовый запрос к приложению
echo "4. Проверка через API приложения:"
curl -s http://10.8.0.1:7777/api/config/celery_status 2>/dev/null || echo "Endpoint не существует"
echo ""

# 5. Проверяем импорты в логах
echo "5. Проверка импортов Celery в логах:"
sudo journalctl -u trading_assistant --since "10 minutes ago" | grep -i "celery\|USE_CELERY" | head -5
echo ""

# 6. Принудительно проверяем конфигурацию
echo "6. Прямая проверка конфигурации:"
cd /home/elcrypto/trading_assistant
source venv/bin/activate
python -c "
from config import Config
print(f'USE_CELERY = {Config.USE_CELERY}')
print(f'Type: {type(Config.USE_CELERY)}')

# Проверяем, что модуль celery_sse_endpoints может импортироваться
if Config.USE_CELERY:
    try:
        from celery_sse_endpoints import analyze_efficiency_celery
        print('✓ celery_sse_endpoints импортируется')
    except ImportError as e:
        print(f'✗ Ошибка импорта celery_sse_endpoints: {e}')
"

echo ""
echo "7. Проверка параметров запроса к SSE:"
echo "В браузере откройте консоль (F12) и проверьте URL запроса."
echo "Должен быть параметр use_celery=true или USE_CELERY должен быть true в конфигурации."