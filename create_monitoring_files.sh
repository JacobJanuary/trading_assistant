#!/bin/bash

# Создание файлов мониторинга прямо на сервере

echo "=== Создание файлов мониторинга ==="

# 1. Создаем trading_assistant_monitor.service
cat > trading_assistant_monitor.service << 'EOF'
[Unit]
Description=Trading Assistant Health Monitor
After=network.target

[Service]
Type=oneshot
User=elcrypto
WorkingDirectory=/home/elcrypto/trading_assistant
Environment="PATH=/home/elcrypto/trading_assistant/venv/bin:/usr/local/bin:/usr/bin:/bin"

# Скрипт проверки и восстановления
ExecStart=/home/elcrypto/trading_assistant/auto_recovery.sh

# Логирование
StandardOutput=append:/home/elcrypto/trading_assistant/logs/monitor.log
StandardError=append:/home/elcrypto/trading_assistant/logs/monitor_error.log
EOF

echo "✅ Создан trading_assistant_monitor.service"

# 2. Создаем trading_assistant_monitor.timer
cat > trading_assistant_monitor.timer << 'EOF'
[Unit]
Description=Run Trading Assistant Health Monitor every 5 minutes
Requires=trading_assistant_monitor.service

[Timer]
# Запуск каждые 5 минут
OnCalendar=*:0/5
# Запуск сразу после загрузки системы
OnBootSec=2min
# Если пропущен запуск (система была выключена), запустить при включении
Persistent=true

[Install]
WantedBy=timers.target
EOF

echo "✅ Создан trading_assistant_monitor.timer"

# 3. Проверяем что файлы созданы
echo ""
echo "Созданные файлы:"
ls -la trading_assistant_monitor.*

echo ""
echo "Теперь запустите install_monitoring.sh для установки"