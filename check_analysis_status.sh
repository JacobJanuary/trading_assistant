#!/bin/bash

echo "=== Проверка статуса анализа эффективности ==="
echo ""

# Используем прямое подключение к БД через psql
export PGPASSWORD='LohNeMamont@!21'

# Получаем последнюю задачу
RESULT=$(psql -U elcrypto -d fox_crypto_new -h 10.8.0.1 -t -A -F'|' -c "
    SELECT
        task_id,
        status,
        progress_current,
        progress_total,
        progress_percent,
        progress_status,
        EXTRACT(EPOCH FROM (NOW() - created_at))::int as age_seconds,
        EXTRACT(EPOCH FROM (NOW() - updated_at))::int as last_update
    FROM web.analysis_tasks
    WHERE task_type='efficiency_analysis'
    ORDER BY created_at DESC
    LIMIT 1
" 2>/dev/null)

if [ -z "$RESULT" ]; then
    echo "Нет активных задач анализа"
    exit 1
fi

# Парсим результат
IFS='|' read -r TASK_ID STATUS CURRENT TOTAL PERCENT MESSAGE AGE_SEC LAST_UPDATE <<< "$RESULT"

echo "Task ID: ${TASK_ID:0:8}..."
echo "Статус: $STATUS"
echo "Прогресс: $CURRENT/$TOTAL ($PERCENT%)"
echo "Сообщение: $MESSAGE"
echo "Начата: $((AGE_SEC / 60)) минут назад"
echo "Последнее обновление: $LAST_UPDATE секунд назад"
echo ""

# Если задача завершена, показываем топ результаты
if [ "$STATUS" = "completed" ]; then
    echo "=== Топ-3 результата ==="
    psql -U elcrypto -d fox_crypto_new -h 10.8.0.1 -t -c "
        SELECT
            row_number() over (order by (r->>'total_pnl')::numeric desc) as rank,
            r->>'score_week' || '/' || r->>'score_month' as filters,
            'PnL: $' || ROUND((r->>'total_pnl')::numeric, 2) as pnl,
            'WR: ' || ROUND((r->>'win_rate')::numeric, 1) || '%' as win_rate,
            'Signals: ' || r->>'total_signals' as signals
        FROM web.analysis_tasks,
             jsonb_array_elements(result_data->'results') as r
        WHERE task_type='efficiency_analysis'
        ORDER BY created_at DESC
        LIMIT 3
    " 2>/dev/null
elif [ "$STATUS" = "running" ]; then
    echo "⏳ Анализ все еще выполняется..."
    echo ""
    echo "Совет: обновите страницу через несколько минут"
    echo "Или используйте этот скрипт для проверки: ./check_analysis_status.sh"
elif [ "$STATUS" = "failed" ]; then
    echo "❌ Анализ завершился с ошибкой"
fi