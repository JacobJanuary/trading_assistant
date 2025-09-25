#!/bin/bash
# Включение Celery на production сервере

echo "=== Включение Celery на production ==="

# Проверяем текущее значение
current_value=$(grep "^USE_CELERY=" .env | cut -d'=' -f2)
echo "Текущее значение USE_CELERY: $current_value"

if [ "$current_value" = "true" ]; then
    echo "✓ Celery уже включен"
else
    echo "Включаем Celery..."
    # Создаем резервную копию
    cp .env .env.backup_$(date +%Y%m%d_%H%M%S)
    
    # Включаем Celery
    sed -i 's/^USE_CELERY=.*/USE_CELERY=true/' .env
    
    echo "✓ Celery включен в .env"
fi

# Проверяем службу Celery
echo ""
echo "Проверка службы Celery..."
if systemctl is-active --quiet celery-worker; then
    echo "✓ Celery worker активен"
else
    echo "✗ Celery worker не активен"
    echo "  Запуск: sudo systemctl start celery-worker"
fi

# Тестируем
echo ""
echo "=== Тестирование Celery ==="
source venv/bin/activate 2>/dev/null || true
python test_celery.py

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Celery работает корректно!"
    echo ""
    echo "Перезапустите приложение для применения изменений:"
    echo "  sudo systemctl restart trading_assistant"
else
    echo ""
    echo "✗ Проблемы с тестированием Celery"
fi