#!/bin/bash
# Полная очистка кеша Python и перезапуск на СЕРВЕРЕ

echo "=== ИСПРАВЛЕНИЕ ПРОБЛЕМЫ С КЕШЕМ PYTHON ==="
echo ""
echo "ВНИМАНИЕ: Этот скрипт для выполнения на СЕРВЕРЕ, не локально!"
echo ""

# 1. Останавливаем все связанные службы
echo "1. Остановка служб..."
sudo systemctl stop trading_assistant
sudo systemctl stop celery-worker

# 2. Очищаем весь Python кеш
echo "2. Очистка Python кеша..."
cd /home/elcrypto/trading_assistant

# Удаляем все .pyc файлы
find . -name "*.pyc" -delete 2>/dev/null
find . -name "*.pyo" -delete 2>/dev/null

# Удаляем все __pycache__ директории
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null

# Удаляем .pytest_cache если есть
rm -rf .pytest_cache 2>/dev/null

echo "   ✓ Кеш очищен"

# 3. Проверяем что изменения есть в файлах
echo "3. Проверка изменений:"
echo -n "   analyze_efficiency_celery(user_id): "
grep -q "def analyze_efficiency_celery(user_id):" celery_sse_endpoints.py && echo "✓" || echo "✗ НЕ НАЙДЕНО!"

echo -n "   вызов с current_user.id: "
grep -q "analyze_efficiency_celery(current_user.id)" app.py && echo "✓" || echo "✗ НЕ НАЙДЕНО!"

# 4. Перезапускаем службы
echo "4. Запуск служб..."
sudo systemctl start celery-worker
sleep 2
sudo systemctl start trading_assistant
sleep 3

# 5. Проверяем статус
echo "5. Проверка статуса:"
echo -n "   Trading Assistant: "
systemctl is-active trading_assistant
echo -n "   Celery Worker: "
systemctl is-active celery-worker
echo -n "   Redis: "
systemctl is-active redis-server

echo ""
echo "=== ГОТОВО ==="
echo ""
echo "Теперь проверьте в браузере:"
echo "1. Откройте страницу анализа эффективности"
echo "2. Запустите анализ"
echo "3. Должно работать через Celery без ошибок 500"
echo ""
echo "Если все еще не работает, выполните:"
echo "  sudo reboot"
echo "для полной перезагрузки сервера"