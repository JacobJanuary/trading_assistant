#!/usr/bin/env python3
"""
Мониторинг сигналов для cron
Запускать каждую минуту: * * * * * /path/to/venv/bin/python /path/to/monitor_signals.py
"""

import os
import sys
from datetime import datetime
from database import Database
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Загружаем переменные окружения из .env
env_path = BASE_DIR / '.env'
load_dotenv(env_path)

# Добавляем путь к проекту в sys.path чтобы импортировать database
sys.path.insert(0, str(BASE_DIR))

from database import Database

def monitor_signals():
    """
    Мониторинг ТОЛЬКО открытых позиций
    """
    db = Database(
        host=os.getenv('DB_HOST'),
        port=os.getenv('DB_PORT'),
        database=os.getenv('DB_NAME'),
        user=os.getenv('DB_USER'),
        password=os.getenv('DB_PASSWORD')
    )

    print(f"[{datetime.now()}] Начало мониторинга сигналов")
    print(
        f"[DEBUG] Подключение к БД: {os.getenv('DB_USER')}@{os.getenv('DB_HOST')}:{os.getenv('DB_PORT')}/{os.getenv('DB_NAME')}")

    try:
        # Получаем открытые позиции
        query = """
            SELECT 
                ws.*,
                tp.id as trading_pair_id
            FROM web.web_signals ws
            JOIN public.trading_pairs tp ON tp.pair_symbol = ws.pair_symbol  -- ИСПРАВЛЕНО!
                AND tp.contract_type_id = 1
                AND tp.exchange_id = 1
            WHERE ws.is_closed = FALSE
        """

        open_positions = db.execute_query(query, fetch=True)

        if not open_positions:
            print("Нет открытых позиций")
            return

        print(f"Найдено {len(open_positions)} открытых позиций")

        closed_count = 0
        updated_count = 0

        for position in open_positions:
            try:
                # Получаем последнюю цену
                price_query = """
                    SELECT mark_price, capture_time
                    FROM public.market_data
                    WHERE trading_pair_id = %s
                    ORDER BY capture_time DESC
                    LIMIT 1
                """

                price_data = db.execute_query(
                    price_query,
                    (position['trading_pair_id'],),
                    fetch=True
                )

                if not price_data:
                    continue

                current_price = float(price_data[0]['mark_price'])
                current_time = price_data[0]['capture_time']
                entry_price = float(position['entry_price'])
                position_size = float(position['position_size_usd'])
                leverage = position['leverage']
                tp_percent = float(position['take_profit_percent'])
                sl_percent = float(position['trailing_stop_percent'])

                # Рассчитываем P&L
                if position['signal_action'] in ['SELL', 'SHORT']:
                    profit_percent = ((entry_price - current_price) / entry_price) * 100
                else:
                    profit_percent = ((current_price - entry_price) / entry_price) * 100

                pnl_usd = position_size * (profit_percent / 100) * leverage

                # Обновляем максимальный профит
                current_max = float(position['max_potential_profit_usd'] or 0)
                new_max = max(current_max, pnl_usd)

                # Проверяем SL/TP
                if profit_percent >= tp_percent:
                    # Закрываем по Take Profit
                    close_query = """
                        UPDATE web.web_signals
                        SET is_closed = TRUE,
                            closing_price = %s,
                            closed_at = %s,
                            close_reason = 'take_profit',
                            realized_pnl_usd = %s,
                            unrealized_pnl_usd = 0,
                            max_potential_profit_usd = %s,
                            last_updated_at = NOW()
                        WHERE signal_id = %s
                    """
                    db.execute_query(close_query, (
                        current_price, current_time, pnl_usd, new_max, position['signal_id']
                    ))
                    closed_count += 1
                    print(f"✅ TP: {position['pair_symbol']} закрыт с прибылью {pnl_usd:.2f}")

                elif profit_percent <= -sl_percent:
                    # Закрываем по Stop Loss
                    close_query = """
                        UPDATE web.web_signals
                        SET is_closed = TRUE,
                            closing_price = %s,
                            closed_at = %s,
                            close_reason = 'stop_loss',
                            realized_pnl_usd = %s,
                            unrealized_pnl_usd = 0,
                            max_potential_profit_usd = %s,
                            last_updated_at = NOW()
                        WHERE signal_id = %s
                    """
                    db.execute_query(close_query, (
                        current_price, current_time, pnl_usd, new_max, position['signal_id']
                    ))
                    closed_count += 1
                    print(f"🛑 SL: {position['pair_symbol']} закрыт с убытком {pnl_usd:.2f}")

                else:
                    # Обновляем открытую позицию
                    update_query = """
                        UPDATE web.web_signals
                        SET unrealized_pnl_usd = %s,
                            last_known_price = %s,
                            max_potential_profit_usd = %s,
                            last_updated_at = NOW()
                        WHERE signal_id = %s
                    """
                    db.execute_query(update_query, (
                        pnl_usd, current_price, new_max, position['signal_id']
                    ))
                    updated_count += 1

            except Exception as e:
                print(f"Ошибка при обработке {position['pair_symbol']}: {e}")
                continue

        print(f"[{datetime.now()}] Завершено: закрыто {closed_count}, обновлено {updated_count}")

    except Exception as e:
        print(f"Ошибка мониторинга: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    monitor_signals()