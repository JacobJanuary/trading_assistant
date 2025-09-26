#!/bin/bash
# Поиск и проверка конфигурации службы

echo "=== Поиск конфигурации службы ==="
echo ""

echo "1. Поиск службы trading_assistant:"
systemctl status trading_assistant --no-pager | head -5

echo ""
echo "2. Путь к файлу службы:"
systemctl show -p FragmentPath trading_assistant

echo ""
echo "3. Содержимое файла службы:"
SERVICE_PATH=$(systemctl show -p FragmentPath trading_assistant | cut -d= -f2)
if [ -f "$SERVICE_PATH" ]; then
    cat "$SERVICE_PATH"
else
    echo "Файл не найден"
fi

echo ""
echo "4. Проверка процесса gunicorn:"
ps aux | grep gunicorn | grep trading | head -3