#!/usr/bin/env python3
from database import Database
import json

db = Database(host='10.8.0.1', port=5432, database='fox_crypto_new', user='elcrypto', password='LohNeMamont@!21')

# Получаем последние завершенные задачи
result = db.execute_query('''
    SELECT task_id, status, progress_current, progress_total, result_data,
           EXTRACT(EPOCH FROM (NOW() - created_at)) as age_seconds
    FROM web.analysis_tasks
    WHERE task_type = 'efficiency_analysis' AND status = 'completed'
    ORDER BY created_at DESC LIMIT 5
''', fetch=True)

print('Последние завершенные задачи:')
for row in result:
    print(f'\nTask ID: {row["task_id"][:8]}...')
    print(f'Progress: {row["progress_current"]}/{row["progress_total"]}')
    print(f'Age: {int(row["age_seconds"]//60)} минут назад')

    if row['result_data']:
        if isinstance(row['result_data'], dict):
            data = row['result_data']
        else:
            data = json.loads(row['result_data'])

        if 'results' in data and data['results']:
            print(f'Results count: {len(data["results"])}')
            # Показываем первые 3 результата
            for i, r in enumerate(data['results'][:3], 1):
                print(f'  {i}. {r.get("score_week", "?")}/{r.get("score_month", "?")}:', end='')
                print(f' PnL=${r.get("total_pnl", 0):.2f},', end='')
                print(f' Signals={r.get("total_signals", 0)},', end='')
                print(f' WR={r.get("win_rate", 0):.1f}%')
        else:
            print('Results: EMPTY or NO results key!')
            print('Data keys:', list(data.keys()) if data else 'None')
            if data:
                print('Full data:', json.dumps(data, indent=2)[:500])
    else:
        print('Result data: NULL')