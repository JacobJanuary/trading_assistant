#!/bin/bash

# Установка автоматического мониторинга для Trading Assistant

echo "=== Установка мониторинга Trading Assistant ==="
echo ""

# 1. Копируем файлы сервисов
echo "1. Установка systemd сервисов..."
sudo cp trading_assistant_monitor.service /etc/systemd/system/
sudo cp trading_assistant_monitor.timer /etc/systemd/system/

# 2. Перезагружаем systemd
echo "2. Перезагрузка systemd..."
sudo systemctl daemon-reload

# 3. Включаем и запускаем timer
echo "3. Активация мониторинга..."
sudo systemctl enable trading_assistant_monitor.timer
sudo systemctl start trading_assistant_monitor.timer

# 4. Проверка статуса
echo ""
echo "4. Статус мониторинга:"
sudo systemctl status trading_assistant_monitor.timer --no-pager

echo ""
echo "5. Следующий запуск:"
sudo systemctl list-timers trading_assistant_monitor.timer

echo ""
echo "=== Установка завершена ==="
echo ""
echo "Мониторинг будет проверять состояние каждые 5 минут и автоматически"
echo "перезапускать сервис при обнаружении проблем."
echo ""
echo "Команды управления:"
echo "  Просмотр логов мониторинга:"
echo "    tail -f /home/elcrypto/trading_assistant/logs/recovery.log"
echo ""
echo "  Ручной запуск проверки:"
echo "    sudo systemctl start trading_assistant_monitor.service"
echo ""
echo "  Остановка мониторинга:"
echo "    sudo systemctl stop trading_assistant_monitor.timer"
echo ""
echo "  Статус мониторинга:"
echo "    sudo systemctl status trading_assistant_monitor.timer"