#!/bin/bash

# Скрипт автоматического восстановления Trading Assistant
# Запускать через cron каждые 5 минут:
# */5 * * * * /home/elcrypto/trading_assistant/auto_recovery.sh

LOG_FILE="/home/elcrypto/trading_assistant/logs/recovery.log"
APP_URL="http://localhost:5000/"
MAX_DB_CONNECTIONS=20
MAX_MEMORY_PERCENT=85

# Функция логирования
log_message() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG_FILE"
}

# Проверка здоровья приложения
check_app_health() {
    response=$(curl -s -o /dev/null -w "%{http_code}" "$APP_URL" --max-time 10)
    if [ "$response" = "200" ] || [ "$response" = "302" ]; then
        return 0
    else
        return 1
    fi
}

# Проверка количества соединений к БД
check_db_connections() {
    connections=$(netstat -an | grep :5432 | grep ESTABLISHED | wc -l)
    if [ "$connections" -gt "$MAX_DB_CONNECTIONS" ]; then
        return 1
    else
        return 0
    fi
}

# Проверка использования памяти
check_memory() {
    memory_percent=$(free | grep Mem | awk '{print int($3/$2 * 100)}')
    if [ "$memory_percent" -gt "$MAX_MEMORY_PERCENT" ]; then
        return 1
    else
        return 0
    fi
}

# Очистка старых логов
cleanup_logs() {
    # Архивируем логи старше 7 дней
    find /home/elcrypto/trading_assistant/logs -name "*.log" -mtime +7 -exec gzip {} \;
    # Удаляем архивы старше 30 дней
    find /home/elcrypto/trading_assistant/logs -name "*.log.gz" -mtime +30 -delete
}

# Основная логика
main() {
    problems_found=false
    
    # Проверка доступности приложения
    if ! check_app_health; then
        log_message "ERROR: Приложение недоступно"
        problems_found=true
    fi
    
    # Проверка соединений к БД
    if ! check_db_connections; then
        log_message "WARNING: Слишком много соединений к БД (>$MAX_DB_CONNECTIONS)"
        problems_found=true
    fi
    
    # Проверка памяти
    if ! check_memory; then
        log_message "WARNING: Высокое использование памяти (>$MAX_MEMORY_PERCENT%)"
        problems_found=true
    fi
    
    # Проверка на критические ошибки в логах
    if [ -f "/home/elcrypto/trading_assistant/logs/gunicorn_error.log" ]; then
        critical_errors=$(tail -100 /home/trading/trading_assistant/logs/gunicorn_error.log | grep -c "decryption failed\|SIGKILL\|MemoryError")
        if [ "$critical_errors" -gt 0 ]; then
            log_message "ERROR: Найдены критические ошибки в логах ($critical_errors)"
            problems_found=true
        fi
    fi
    
    # Если найдены проблемы, пытаемся восстановить
    if [ "$problems_found" = true ]; then
        log_message "INFO: Начинаем процедуру восстановления"
        
        # Попытка мягкого перезапуска
        systemctl reload trading_assistant
        sleep 10
        
        if ! check_app_health; then
            log_message "WARNING: Мягкий перезапуск не помог, выполняем полный перезапуск"
            
            # Остановка старых процессов
            systemctl stop trading_assistant
            sleep 5
            
            # Убиваем зависшие процессы Gunicorn если остались
            pkill -9 -f "gunicorn.*app:app" 2>/dev/null
            
            # Очистка временных файлов
            rm -f /dev/shm/wgunicorn-* 2>/dev/null
            
            # Запуск сервиса
            systemctl start trading_assistant
            sleep 10
            
            if check_app_health; then
                log_message "SUCCESS: Приложение успешно восстановлено"
            else
                log_message "CRITICAL: Не удалось восстановить приложение, требуется ручное вмешательство"
                
                # Отправка уведомления (если настроено)
                # echo "Trading Assistant недоступен и не может быть автоматически восстановлен" | mail -s "CRITICAL: Trading Assistant Down" admin@example.com
            fi
        else
            log_message "SUCCESS: Мягкий перезапуск успешен"
        fi
    fi
    
    # Очистка логов раз в день (в полночь)
    hour=$(date +%H)
    if [ "$hour" = "00" ]; then
        cleanup_logs
        log_message "INFO: Выполнена очистка старых логов"
    fi
}

# Запуск главной функции
main