#!/bin/bash

# Скрипт настройки cron задач для Trading Assistant

echo "=== Настройка автоматических задач для Trading Assistant ==="

# Путь к приложению
APP_PATH="/home/elcrypto/trading_assistant"

# Создание директории для логов если не существует
mkdir -p "$APP_PATH/logs"

# Делаем скрипты исполняемыми
chmod +x "$APP_PATH/auto_recovery.sh"
chmod +x "$APP_PATH/monitor_production.py"
chmod +x "$APP_PATH/diagnose_production.sh"

# Создание cron задач
CRON_JOBS="
# Trading Assistant автоматические задачи

# Проверка здоровья и автовосстановление каждые 5 минут
*/5 * * * * $APP_PATH/auto_recovery.sh >/dev/null 2>&1

# Мониторинг системы каждые 15 минут, сохранение отчета
*/15 * * * * $APP_PATH/venv/bin/python $APP_PATH/monitor_production.py >> $APP_PATH/logs/monitor.log 2>&1

# Диагностика системы каждый день в 3:00
0 3 * * * $APP_PATH/diagnose_production.sh >> $APP_PATH/logs/diagnosis.log 2>&1

# Ротация логов каждое воскресенье в 2:00
0 2 * * 0 find $APP_PATH/logs -name '*.log' -size +100M -exec truncate -s 0 {} \;

# Перезапуск приложения каждую ночь в 4:00 для очистки памяти (опционально)
# 0 4 * * * systemctl restart trading_assistant
"

echo "Добавление следующих задач в crontab:"
echo "$CRON_JOBS"

# Проверка, установлены ли уже задачи
if crontab -l 2>/dev/null | grep -q "Trading Assistant"; then
    echo "⚠️  Задачи Trading Assistant уже установлены в crontab"
    read -p "Хотите обновить их? (y/n): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        # Удаляем старые задачи
        (crontab -l 2>/dev/null | grep -v "Trading Assistant" | grep -v "$APP_PATH") | crontab -
        # Добавляем новые
        (crontab -l 2>/dev/null; echo "$CRON_JOBS") | crontab -
        echo "✅ Cron задачи обновлены"
    else
        echo "❌ Отменено"
    fi
else
    # Добавляем задачи
    (crontab -l 2>/dev/null; echo "$CRON_JOBS") | crontab -
    echo "✅ Cron задачи установлены"
fi

echo ""
echo "Текущие cron задачи:"
crontab -l | grep -A 10 "Trading Assistant"

echo ""
echo "=== Настройка завершена ==="
echo ""
echo "Команды для управления:"
echo "  - Просмотр задач: crontab -l"
echo "  - Редактирование: crontab -e"
echo "  - Удаление всех задач: crontab -r"
echo ""
echo "Проверка логов:"
echo "  - Автовосстановление: tail -f $APP_PATH/logs/recovery.log"
echo "  - Мониторинг: tail -f $APP_PATH/logs/monitor.log"
echo "  - Диагностика: tail -f $APP_PATH/logs/diagnosis.log"