#!/bin/bash
# Скрипт развертывания на production сервере

echo "=== Развертывание Trading Assistant на production ==="

# Проверяем что мы на production сервере
if [ ! -f /etc/systemd/system/celery-worker.service ]; then
    echo "Ошибка: Celery worker service не найден. Вы на production сервере?"
    exit 1
fi


# Проверяем Redis
echo "Проверка Redis..."
if ! systemctl is-active --quiet redis-server; then
    echo "Запуск Redis..."
    sudo systemctl start redis-server
fi

# Перезапускаем Celery для загрузки новой конфигурации
echo "Перезапуск Celery workers..."
sudo systemctl restart celery-worker

# Ждем пока Celery загрузится
sleep 3

# Проверяем статус
echo ""
echo "=== Проверка статуса служб ==="
echo "Redis: $(systemctl is-active redis-server)"
echo "Celery: $(systemctl is-active celery-worker)"

# Тестируем Celery
echo ""
echo "=== Тестирование Celery ==="
python test_celery.py

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Celery настроен и работает корректно!"
    echo ""
    echo "Теперь можно перезапустить приложение:"
    echo "  sudo systemctl restart trading_assistant"
else
    echo ""
    echo "✗ Проблемы с Celery. Проверьте логи:"
    echo "  sudo journalctl -u celery -n 50"
fi