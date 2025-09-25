#!/bin/bash
# Скрипт для исправления Celery service на production

echo "=== Исправление Celery Worker Service ==="

# Проверяем что мы под правильным пользователем
if [ "$USER" != "elcrypto" ]; then
    echo "Ошибка: Запустите скрипт под пользователем elcrypto"
    exit 1
fi

# Останавливаем текущий сервис
echo "Остановка текущего Celery worker..."
sudo systemctl stop celery-worker

# Резервная копия старого файла
echo "Создание резервной копии..."
sudo cp /etc/systemd/system/celery-worker.service /etc/systemd/system/celery-worker.service.backup

# Обновляем файл службы
echo "Обновление файла службы..."
sudo cp celery-worker-fixed.service /etc/systemd/system/celery-worker.service

# Перезагружаем systemd
echo "Перезагрузка systemd..."
sudo systemctl daemon-reload

# Запускаем новый сервис
echo "Запуск обновленного Celery worker..."
sudo systemctl start celery-worker

# Ждем запуска
sleep 3

# Проверяем статус
echo ""
echo "=== Проверка статуса ==="
sudo systemctl status celery-worker --no-pager | head -20

# Проверяем логи
echo ""
echo "=== Последние логи ==="
sudo journalctl -u celery-worker -n 20 --no-pager

# Тестируем
echo ""
echo "=== Тестирование Celery ==="
cd /home/elcrypto/trading_assistant
source venv/bin/activate
python test_celery.py

if [ $? -eq 0 ]; then
    echo ""
    echo "✓ Celery исправлен и работает!"
    echo ""
    echo "Теперь можно использовать приложение с асинхронной обработкой."
else
    echo ""
    echo "✗ Все еще есть проблемы. Проверьте логи:"
    echo "  sudo journalctl -u celery-worker -f"
fi