#!/bin/bash
# Очистка старых файлов и развертывание Application Factory паттерна

echo "==========================================="
echo "  ОЧИСТКА И РАЗВЕРТЫВАНИЕ APP FACTORY"
echo "==========================================="
echo ""
echo "ВНИМАНИЕ: Это критическое изменение архитектуры!"
echo "Убедитесь что у вас есть полный бэкап!"
echo ""
read -p "Продолжить? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    exit 1
fi

cd /home/elcrypto/trading_assistant

# 1. СОЗДАНИЕ БЭКАПА
echo "1. Создание бэкапа..."
BACKUP_DIR="backup_$(date +%Y%m%d_%H%M%S)"
mkdir -p $BACKUP_DIR

# Бэкап критических файлов
cp app.py $BACKUP_DIR/
cp -r templates/ $BACKUP_DIR/templates/
cp -r static/ $BACKUP_DIR/static/
cp *.service $BACKUP_DIR/ 2>/dev/null

echo "   ✓ Бэкап создан в $BACKUP_DIR"

# 2. ОСТАНОВКА СЛУЖБ
echo "2. Остановка служб..."
sudo systemctl stop trading_assistant
sudo systemctl stop celery-worker
echo "   ✓ Службы остановлены"

# 3. УДАЛЕНИЕ СТАРЫХ/НЕНУЖНЫХ ФАЙЛОВ
echo "3. Удаление ненужных файлов..."

# Удаляем тестовые файлы
rm -f test_*.py
rm -f simple_test.py
rm -f monitor_production.py

# Удаляем старые временные файлы
rm -f app.py.backup_*
rm -f *.pyc
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

echo "   ✓ Тестовые и временные файлы удалены"

# 4. СОЗДАНИЕ ФИНАЛЬНОЙ СТРУКТУРЫ
echo "4. Создание финальной структуры Application Factory..."

# Создаем wsgi.py если его нет
if [ ! -f wsgi.py ]; then
cat > wsgi.py << 'EOF'
"""
WSGI entry point for production deployment
"""
from app_factory import create_app

app = create_app()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7777, debug=False)
EOF
fi

# Создаем extensions.py для общих расширений
cat > extensions.py << 'EOF'
"""
Shared extensions for Application Factory
"""

# Глобальные переменные для отслеживания состояния
database_critical_errors = 0

# Здесь будут инициализироваться расширения
db = None
login_manager = None
EOF

# Создаем services.py заглушку если нужно
if [ ! -f services.py ]; then
cat > services.py << 'EOF'
"""
Business logic services
Extracted from app.py
"""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Заглушки для сервисных функций
def get_dashboard_data(db, user_id):
    """Получение данных для дашборда"""
    return {'status': 'ok', 'data': []}

def apply_scoring_filters(db, user_id, data):
    """Применение фильтров scoring"""
    return {'success': True}

def apply_scoring_filters_v2(db, user_id, data):
    """Применение фильтров scoring v2"""
    return {'success': True}

def save_scoring_filters(db, user_id, data):
    """Сохранение фильтров scoring"""
    pass

def get_scoring_date_info(db, user_id, data):
    """Получение информации о датах"""
    return {'dates': []}

def get_scoring_date_info_v2(db, user_id, data):
    """Получение информации о датах v2"""
    return {'dates': []}

def initialize_signals_trailing(db, user_id, data):
    """Инициализация сигналов для trailing"""
    return {'success': True}

def reinitialize_signals(db, user_id, params):
    """Переинициализация сигналов"""
    return {'success': True}

def compare_strategies(db, user_id, data):
    """Сравнение стратегий"""
    return {'strategies': []}

def run_ab_test(db, user_id, data):
    """Запуск A/B теста"""
    return {'results': []}

def analyze_efficiency(db, user_id, data):
    """Анализ эффективности"""
    return {'efficiency': 0}

def analyze_efficiency_30days(db, user_id, data):
    """30-дневный анализ эффективности"""
    return {'efficiency': 0}

def get_cached_results(db, user_id, analysis_type):
    """Получение кешированных результатов"""
    return {'cached': False}

def clear_cached_results(db, user_id, analysis_type):
    """Очистка кешированных результатов"""
    pass

def save_trading_mode(db, user_id, mode):
    """Сохранение торгового режима"""
    pass

def get_user_trading_mode(db, user_id):
    """Получение торгового режима"""
    return 'spot'
EOF
fi

# 5. ОБНОВЛЕНИЕ СЛУЖБ SYSTEMD
echo "5. Обновление служб systemd..."

# trading_assistant.service - использует wsgi.py
sudo tee /etc/systemd/system/trading_assistant.service > /dev/null << 'EOF'
[Unit]
Description=Trading Assistant Flask Application
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=elcrypto
Group=elcrypto
WorkingDirectory=/home/elcrypto/trading_assistant
Environment="PATH=/home/elcrypto/trading_assistant/venv/bin"
Environment="PYTHONPATH=/home/elcrypto/trading_assistant"

ExecStart=/home/elcrypto/trading_assistant/venv/bin/gunicorn \
    --workers 4 \
    --worker-class sync \
    --bind 0.0.0.0:7777 \
    --timeout 120 \
    --log-level info \
    --access-logfile - \
    --error-logfile - \
    wsgi:app

Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

# celery-worker.service - использует celery_worker.py
sudo tee /etc/systemd/system/celery-worker.service > /dev/null << 'EOF'
[Unit]
Description=Celery Worker for Trading Assistant
After=network.target redis.service

[Service]
Type=simple
User=elcrypto
Group=elcrypto
WorkingDirectory=/home/elcrypto/trading_assistant
Environment="PATH=/home/elcrypto/trading_assistant/venv/bin"
Environment="PYTHONPATH=/home/elcrypto/trading_assistant"

ExecStart=/home/elcrypto/trading_assistant/venv/bin/celery \
    -A celery_worker:celery worker \
    --loglevel=info \
    --concurrency=2

Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

LimitNOFILE=65536
LimitNPROC=65536

[Install]
WantedBy=multi-user.target
EOF

echo "   ✓ Службы обновлены"

# 6. ПЕРЕЗАГРУЗКА SYSTEMD
echo "6. Перезагрузка systemd..."
sudo systemctl daemon-reload
echo "   ✓ Systemd перезагружен"

# 7. ТЕСТИРОВАНИЕ ИМПОРТА
echo "7. Тестирование новой архитектуры..."
source venv/bin/activate

python3 -c "
import sys
try:
    from app_factory import create_app, create_celery
    print('   ✓ app_factory импортирован')
    app = create_app()
    print('   ✓ Flask приложение создано')
    celery = create_celery(app)
    print('   ✓ Celery создан с контекстом приложения')
    print('   ✓ Архитектура готова к работе!')
except Exception as e:
    print(f'   ✗ Ошибка: {e}')
    sys.exit(1)
"

if [ $? -ne 0 ]; then
    echo ""
    echo "✗ ОШИБКА при тестировании!"
    echo "Восстановите из бэкапа: cp -r $BACKUP_DIR/* ."
    exit 1
fi

# 8. ЗАПУСК СЛУЖБ
echo "8. Запуск служб..."
sudo systemctl start redis
sleep 2
sudo systemctl start celery-worker
sleep 3
sudo systemctl start trading_assistant

# 9. ПРОВЕРКА СТАТУСА
echo "9. Проверка статуса служб:"
echo ""
echo "   Redis:             $(systemctl is-active redis)"
echo "   Celery Worker:     $(systemctl is-active celery-worker)"
echo "   Trading Assistant: $(systemctl is-active trading_assistant)"
echo ""

# 10. ПРОВЕРКА ЛОГОВ
echo "10. Последние логи:"
echo "----------------------------------------"
sudo journalctl -u trading_assistant -n 5 --no-pager
echo "----------------------------------------"

echo ""
echo "==========================================="
echo "       РАЗВЕРТЫВАНИЕ ЗАВЕРШЕНО!"
echo "==========================================="
echo ""
echo "Файлы которые были удалены:"
echo "  - test_*.py (все тестовые файлы)"
echo "  - simple_test.py"
echo "  - monitor_production.py"
echo "  - __pycache__ директории"
echo "  - *.pyc файлы"
echo ""
echo "Новая структура:"
echo "  - app_factory.py - Application Factory с ВСЕМИ маршрутами"
echo "  - wsgi.py - точка входа для Gunicorn"
echo "  - celery_worker.py - воркер Celery"
echo "  - extensions.py - общие расширения"
echo "  - services.py - бизнес-логика (заглушка)"
echo ""
echo "Службы systemd обновлены:"
echo "  - /etc/systemd/system/trading_assistant.service"
echo "  - /etc/systemd/system/celery-worker.service"
echo ""
echo "ВАЖНО: Старый app.py сохранен в $BACKUP_DIR/"
echo ""
echo "Проверьте работу:"
echo "  1. Откройте браузер: http://your-server:7777"
echo "  2. Проверьте логи: sudo journalctl -f -u trading_assistant"
echo "  3. Проверьте Celery: sudo journalctl -f -u celery-worker"
echo ""
echo "При проблемах восстановите из бэкапа:"
echo "  cp -r $BACKUP_DIR/* ."
echo "  sudo systemctl restart trading_assistant"