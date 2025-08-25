#!/usr/bin/env python3
"""
Combinations Win Rate Analyzer v1.0
Основан на проверенном pattern_wr_analyzer.py
Анализирует эффективность комбинированных сигналов для LONG и SHORT позиций
"""

import os
import sys
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import time
from typing import List, Dict, Optional, Tuple
import numpy as np

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('combinations_wr_analyzer.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Параметры подключения к БД
DB_CONFIG = {
    'host': 'localhost',
    'port': 5432,
    'dbname': 'fox_crypto',
    'user': 'elcrypto',
    'password': 'LohNeMamont@!21'
}

# Параметры анализа
ANALYSIS_PARAMS = {
    'tp_percent': 4.0,
    'sl_percent': 3.0,
    'position_size': 100.0,
    'leverage': 5,
    'analysis_hours': 48,
    'entry_delay_minutes': 15
}


class CombinationsWinRateAnalyzer:
    def __init__(self, db_config: dict, recreate_table: bool = False):
        self.db_config = db_config
        self.recreate_table = recreate_table
        self.conn = None
        self.processed_count = 0
        self.error_count = 0
        self.new_combinations_count = 0
        self.skipped_count = 0

    def connect(self):
        """Подключение к БД"""
        try:
            conn_string = f"host={self.db_config['host']} port={self.db_config['port']} " \
                          f"dbname={self.db_config['dbname']} user={self.db_config['user']} " \
                          f"password={self.db_config['password']}"
            self.conn = psycopg.connect(conn_string, row_factory=dict_row)
            logger.info("✅ Успешное подключение к БД")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise

    def disconnect(self):
        """Отключение от БД"""
        if self.conn:
            self.conn.close()
            logger.info("🔌 Отключение от БД")

    def create_result_table(self):
        """Создание таблицы для результатов"""
        try:
            with self.conn.cursor() as cur:
                if self.recreate_table:
                    # Удаляем старую таблицу если требуется пересоздание
                    logger.info("🗑️ Удаление старой таблицы fas.test_combinations_wr...")
                    cur.execute("DROP TABLE IF EXISTS fas.test_combinations_wr CASCADE")
                    self.conn.commit()

                # Проверяем существование таблицы
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'fas' 
                        AND table_name = 'test_combinations_wr'
                    )
                """)
                table_exists = cur.fetchone()['exists']

                if not table_exists:
                    # Создаем новую таблицу
                    logger.info("📝 Создание таблицы fas.test_combinations_wr...")
                    create_table_query = """
                    CREATE TABLE fas.test_combinations_wr (
                        id BIGINT PRIMARY KEY,
                        combination_id INTEGER,
                        trading_pair_id INTEGER NOT NULL,
                        timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                        timeframe VARCHAR(10) NOT NULL,
                        component_patterns JSONB NOT NULL,
                        combined_score DECIMAL(10,2) NOT NULL,
                        combined_confidence DECIMAL(10,2) NOT NULL,
                        signal_direction VARCHAR(10),
                        strength_rating VARCHAR(20),

                        -- Результаты для SHORT
                        sell_tp BOOLEAN DEFAULT FALSE,
                        sell_sl BOOLEAN DEFAULT FALSE,
                        sell_result BOOLEAN,

                        -- Результаты для LONG  
                        buy_tp BOOLEAN DEFAULT FALSE,
                        buy_sl BOOLEAN DEFAULT FALSE,
                        buy_result BOOLEAN,

                        -- Метаданные
                        processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
                    )
                    """
                    cur.execute(create_table_query)

                    # Создаем индексы
                    cur.execute("""
                        CREATE INDEX idx_combinations_wr_strength 
                        ON fas.test_combinations_wr(strength_rating)
                    """)
                    cur.execute("""
                        CREATE INDEX idx_combinations_wr_combination 
                        ON fas.test_combinations_wr(combination_id)
                    """)

                    self.conn.commit()
                    logger.info("✅ Таблица fas.test_combinations_wr создана успешно")
                else:
                    logger.info("✅ Таблица fas.test_combinations_wr уже существует")

        except Exception as e:
            logger.error(f"❌ Ошибка создания таблицы: {e}")
            self.conn.rollback()
            raise

    def get_unprocessed_combinations(self, batch_size: int = 10000) -> List[Dict]:
        """Получение пакета необработанных комбинированных сигналов старше 48 часов"""
        query = """
            SELECT 
                cs.id,
                cs.combination_id,
                cs.trading_pair_id,
                cs.timestamp,
                cs.timeframe::TEXT as timeframe,
                cs.component_patterns,
                cs.combined_score,
                cs.combined_confidence,
                cs.signal_direction,
                cs.strength_rating,
                tp.pair_symbol
            FROM fas.combined_signals cs
            JOIN public.trading_pairs tp ON cs.trading_pair_id = tp.id
            WHERE cs.timestamp <= NOW() - INTERVAL '48 hours'
                AND NOT EXISTS (
                    SELECT 1 FROM fas.test_combinations_wr tcw
                    WHERE tcw.id = cs.id
                )
            ORDER BY cs.timestamp ASC
            LIMIT %s
        """

        with self.conn.cursor() as cur:
            cur.execute(query, (batch_size,))
            combinations = cur.fetchall()

        return combinations

    def get_total_unprocessed_count(self) -> int:
        """Получение общего количества необработанных комбинаций"""
        query = """
            SELECT COUNT(*) as count
            FROM fas.combined_signals cs
            WHERE cs.timestamp <= NOW() - INTERVAL '48 hours'
                AND NOT EXISTS (
                    SELECT 1 FROM fas.test_combinations_wr tcw
                    WHERE tcw.id = cs.id
                )
        """

        with self.conn.cursor() as cur:
            cur.execute(query)
            result = cur.fetchone()
            return result['count'] if result else 0

    def get_entry_price(self, trading_pair_id: int, signal_time: datetime,
                        signal_type: str) -> Optional[Dict]:
        """Получение цены входа из первой свечи после signal_time + 15 минут"""
        entry_time = signal_time + timedelta(minutes=ANALYSIS_PARAMS['entry_delay_minutes'])

        query = """
            SELECT 
                timestamp,
                close_price,
                high_price,
                low_price
            FROM fas.market_data_aggregated
            WHERE trading_pair_id = %s
                AND timeframe = '15m'
                AND timestamp >= %s
            ORDER BY timestamp ASC
            LIMIT 1
        """

        try:
            with self.conn.cursor() as cur:
                cur.execute(query, (trading_pair_id, entry_time))
                result = cur.fetchone()

            if result:
                # Для LONG используем high_price (худший вход), для SHORT - low_price
                if signal_type == 'LONG':
                    entry_price = float(result['high_price'])
                else:  # SHORT
                    entry_price = float(result['low_price'])

                return {
                    'entry_price': entry_price,
                    'entry_time': result['timestamp']
                }
            return None

        except Exception as e:
            logger.error(f"❌ Ошибка получения цены входа: {e}")
            return None

    def analyze_position(self, signal_type: str, entry_price: float,
                         history: List[Dict], actual_entry_time) -> Dict:
        """Анализ позиции на исторических данных"""
        tp_percent = ANALYSIS_PARAMS['tp_percent']
        sl_percent = ANALYSIS_PARAMS['sl_percent']

        # Расчет уровней TP и SL
        if signal_type == 'LONG':
            tp_price = entry_price * (1 + tp_percent / 100)
            sl_price = entry_price * (1 - sl_percent / 100)
        else:  # SHORT
            tp_price = entry_price * (1 - tp_percent / 100)
            sl_price = entry_price * (1 + sl_percent / 100)

        # Переменные для отслеживания результата
        result = None
        tp_hit = False
        sl_hit = False

        # Проходим по истории цен
        for candle in history:
            high_price = float(candle['high_price'])
            low_price = float(candle['low_price'])

            if signal_type == 'LONG':
                # Сначала проверяем SL
                if low_price <= sl_price:
                    result = False
                    sl_hit = True
                    break
                # Затем проверяем TP
                elif high_price >= tp_price:
                    result = True
                    tp_hit = True
                    break
            else:  # SHORT
                # Сначала проверяем SL
                if high_price >= sl_price:
                    result = False
                    sl_hit = True
                    break
                # Затем проверяем TP
                elif low_price <= tp_price:
                    result = True
                    tp_hit = True
                    break

        return {
            'result': result,
            'tp': tp_hit,
            'sl': sl_hit
        }

    def analyze_combination(self, combination: Dict) -> Optional[Dict]:
        """Анализ одной комбинации для LONG и SHORT позиций"""
        try:
            # Базовая структура результата
            base_result = {
                'id': combination['id'],
                'combination_id': combination['combination_id'],
                'trading_pair_id': combination['trading_pair_id'],
                'timestamp': combination['timestamp'],
                'timeframe': combination['timeframe'],
                'component_patterns': combination['component_patterns'],
                'combined_score': float(combination['combined_score']),
                'combined_confidence': float(combination['combined_confidence']),
                'signal_direction': combination['signal_direction'],
                'strength_rating': combination['strength_rating'],
                'sell_tp': False,
                'sell_sl': False,
                'sell_result': None,
                'buy_tp': False,
                'buy_sl': False,
                'buy_result': None
            }

            # Получаем цену входа для LONG
            long_entry_data = self.get_entry_price(
                combination['trading_pair_id'],
                combination['timestamp'],
                'LONG'
            )

            # Получаем цену входа для SHORT
            short_entry_data = self.get_entry_price(
                combination['trading_pair_id'],
                combination['timestamp'],
                'SHORT'
            )

            if not long_entry_data or not short_entry_data:
                logger.warning(f"⚠️ Нет цены входа для комбинации {combination['id']}")
                self.skipped_count += 1
                return base_result  # Возвращаем с NULL результатами

            # Получаем историю цен за 48 часов
            history_query = """
                SELECT 
                    timestamp,
                    close_price,
                    high_price,
                    low_price
                FROM fas.market_data_aggregated
                WHERE trading_pair_id = %s
                    AND timeframe = '15m'
                    AND timestamp >= %s
                    AND timestamp <= %s + INTERVAL '48 hours'
                ORDER BY timestamp ASC
            """

            with self.conn.cursor() as cur:
                cur.execute(history_query, (
                    combination['trading_pair_id'],
                    long_entry_data['entry_time'],
                    long_entry_data['entry_time']
                ))
                history = cur.fetchall()

            if not history or len(history) < 10:
                logger.warning(f"⚠️ Недостаточно истории для комбинации {combination['id']}")
                self.skipped_count += 1
                return base_result  # Возвращаем с NULL результатами

            # Анализируем LONG позицию
            long_results = self.analyze_position(
                'LONG',
                long_entry_data['entry_price'],
                history[1:],  # Пропускаем первую свечу
                long_entry_data['entry_time']
            )

            # Анализируем SHORT позицию
            short_results = self.analyze_position(
                'SHORT',
                short_entry_data['entry_price'],
                history[1:],
                short_entry_data['entry_time']
            )

            # Обновляем результат
            base_result['sell_tp'] = short_results['tp']
            base_result['sell_sl'] = short_results['sl']
            base_result['sell_result'] = short_results['result']
            base_result['buy_tp'] = long_results['tp']
            base_result['buy_sl'] = long_results['sl']
            base_result['buy_result'] = long_results['result']

            return base_result

        except Exception as e:
            logger.error(f"❌ Ошибка анализа комбинации {combination['id']}: {e}")
            self.error_count += 1
            return None

    def save_results(self, results: List[Dict]):
        """Сохранение результатов в БД"""
        if not results:
            return

        insert_query = """
            INSERT INTO fas.test_combinations_wr (
                id, combination_id, trading_pair_id, timestamp, timeframe,
                component_patterns, combined_score, combined_confidence,
                signal_direction, strength_rating,
                sell_tp, sell_sl, sell_result,
                buy_tp, buy_sl, buy_result
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (id) DO UPDATE SET
                sell_result = EXCLUDED.sell_result,
                buy_result = EXCLUDED.buy_result,
                processed_at = NOW()
        """

        saved_count = 0

        with self.conn.cursor() as cur:
            for result in results:
                try:
                    import json
                    cur.execute(insert_query, (
                        result['id'],
                        result['combination_id'],
                        result['trading_pair_id'],
                        result['timestamp'],
                        result['timeframe'],
                        json.dumps(result['component_patterns']) if result['component_patterns'] else None,
                        result['combined_score'],
                        result['combined_confidence'],
                        result['signal_direction'],
                        result['strength_rating'],
                        result['sell_tp'],
                        result['sell_sl'],
                        result['sell_result'],
                        result['buy_tp'],
                        result['buy_sl'],
                        result['buy_result']
                    ))
                    saved_count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка сохранения результата для комбинации {result['id']}: {e}")
                    self.error_count += 1

        self.conn.commit()
        self.new_combinations_count += saved_count
        logger.info(f"💾 Сохранено {saved_count} результатов из {len(results)}")

    def print_statistics(self):
        """Вывод статистики по результатам анализа"""
        try:
            # Первый запрос - статистика по силе сигнала
            stats_query = """
                WITH combination_stats AS (
                    SELECT 
                        strength_rating,
                        COUNT(*) as total_combinations,
                        COUNT(CASE WHEN buy_result = true THEN 1 END) as long_wins,
                        COUNT(CASE WHEN buy_result = false THEN 1 END) as long_losses,
                        COUNT(CASE WHEN sell_result = true THEN 1 END) as short_wins,
                        COUNT(CASE WHEN sell_result = false THEN 1 END) as short_losses
                    FROM fas.test_combinations_wr
                    WHERE processed_at >= NOW() - INTERVAL '1 day'
                    GROUP BY strength_rating
                    ORDER BY total_combinations DESC
                )
                SELECT * FROM combination_stats
            """

            with self.conn.cursor() as cur:
                cur.execute(stats_query)
                combination_stats = cur.fetchall()

            logger.info("\n" + "=" * 70)
            logger.info("📊 СТАТИСТИКА WIN RATE ПО КОМБИНАЦИЯМ")
            logger.info("=" * 70)

            for stat in combination_stats:
                strength = stat['strength_rating'] or 'UNKNOWN'
                total = stat['total_combinations']

                long_total = stat['long_wins'] + stat['long_losses']
                long_wr = (stat['long_wins'] / long_total * 100) if long_total > 0 else 0

                short_total = stat['short_wins'] + stat['short_losses']
                short_wr = (stat['short_wins'] / short_total * 100) if short_total > 0 else 0

                logger.info(f"\n📈 Сила сигнала: {strength}")
                logger.info(f"   Всего комбинаций: {total}")
                logger.info(f"   ├─ LONG Win Rate: {long_wr:.1f}% ({stat['long_wins']}/{long_total})")
                logger.info(f"   └─ SHORT Win Rate: {short_wr:.1f}% ({stat['short_wins']}/{short_total})")

            # Второй запрос - статистика по направлениям (создаем новый курсор)
            direction_query = """
                SELECT 
                    signal_direction,
                    COUNT(*) as total_signals,
                    COUNT(CASE WHEN 
                        (signal_direction = 'BUY' AND buy_result = true) OR 
                        (signal_direction = 'SELL' AND sell_result = true) 
                    THEN 1 END) as correct_predictions,
                    COUNT(CASE WHEN 
                        (signal_direction = 'BUY' AND buy_result = false) OR 
                        (signal_direction = 'SELL' AND sell_result = false) 
                    THEN 1 END) as wrong_predictions
                FROM fas.test_combinations_wr
                WHERE processed_at >= NOW() - INTERVAL '1 day'
                    AND signal_direction IS NOT NULL
                GROUP BY signal_direction
            """

            with self.conn.cursor() as cur2:
                cur2.execute(direction_query)
                direction_stats = cur2.fetchall()

            if direction_stats:
                logger.info("\n" + "=" * 70)
                logger.info("📊 ТОЧНОСТЬ ПРЕДСКАЗАНИЯ НАПРАВЛЕНИЯ")
                logger.info("=" * 70)

                for stat in direction_stats:
                    direction = stat['signal_direction']
                    total = stat['correct_predictions'] + stat['wrong_predictions']
                    if total > 0:
                        accuracy = (stat['correct_predictions'] / total) * 100
                        logger.info(f"{direction}: {accuracy:.1f}% точность ({stat['correct_predictions']}/{total})")

            logger.info("=" * 70)

        except Exception as e:
            logger.error(f"❌ Ошибка при выводе статистики: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def run(self):
        """Основной процесс анализа"""
        start_time = datetime.now()
        logger.info("🚀 Начало анализа комбинированных сигналов")
        logger.info(f"📅 Время запуска: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"⚙️ Параметры: TP={ANALYSIS_PARAMS['tp_percent']}%, SL={ANALYSIS_PARAMS['sl_percent']}%")

        try:
            self.connect()
            logger.info("📝 Проверка/создание таблицы результатов...")
            self.create_result_table()

            logger.info("🔍 Подсчет необработанных комбинаций...")
            total_unprocessed = self.get_total_unprocessed_count()

            if total_unprocessed == 0:
                logger.info("✅ Нет новых комбинаций для обработки")
                return

            logger.info(f"📊 Всего необработанных комбинаций: {total_unprocessed}")

            batch_size = 10000
            save_batch_size = 100
            batch_number = 0

            while True:
                batch_number += 1
                logger.info(f"\n🔄 Проверка необработанных комбинаций...")
                current_unprocessed = self.get_total_unprocessed_count()

                if current_unprocessed == 0:
                    logger.info("✅ Все комбинации обработаны!")
                    break

                logger.info(f"\n📦 Обработка пакета #{batch_number}")
                logger.info(f"📊 Осталось необработанных: {current_unprocessed}")

                logger.info(f"📥 Загрузка пакета комбинаций (до {batch_size} штук)...")
                combinations = self.get_unprocessed_combinations(batch_size)

                if not combinations:
                    logger.info(f"✅ Больше нет комбинаций для обработки")
                    break

                logger.info(f"📊 В пакете #{batch_number}: {len(combinations)} комбинаций")

                # Для отладки - показываем ID первых комбинаций
                if len(combinations) <= 30:
                    combination_ids = [c['id'] for c in combinations]
                    logger.info(f"🔍 ID комбинаций в пакете: {combination_ids}")

                results = []
                batch_processed = 0
                batch_skipped = 0

                for i, combination in enumerate(combinations):
                    if i % 100 == 0 and i > 0:
                        progress = (i / len(combinations)) * 100
                        logger.info(f"⏳ Пакет #{batch_number}: {i}/{len(combinations)} ({progress:.1f}%)")

                    result = self.analyze_combination(combination)
                    if result:
                        results.append(result)
                        batch_processed += 1
                        self.processed_count += 1
                    else:
                        batch_skipped += 1

                    if len(results) >= save_batch_size:
                        self.save_results(results)
                        results = []

                if results:
                    self.save_results(results)

                logger.info(f"✅ Пакет #{batch_number} обработан: {batch_processed} успешно, {batch_skipped} пропущено")

                # Защита от зацикливания
                if batch_processed == 0 and batch_skipped == 0:
                    logger.error(f"❌ Пакет #{batch_number} не содержал обрабатываемых комбинаций! Прерывание.")
                    break

                if current_unprocessed > batch_size:
                    time.sleep(2)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info("\n" + "=" * 70)
            logger.info("📋 ИТОГИ ОБРАБОТКИ:")
            logger.info("=" * 70)
            logger.info(f"✅ Успешно обработано: {self.processed_count}")
            logger.info(f"💾 Сохранено новых результатов: {self.new_combinations_count}")
            logger.info(f"⭕ Пропущено: {self.skipped_count}")
            logger.info(f"❌ Ошибок: {self.error_count}")
            logger.info(f"⏱️ Время выполнения: {duration:.1f} секунд ({duration / 60:.1f} минут)")

            if self.processed_count > 0:
                logger.info(f"⚡ Скорость обработки: {self.processed_count / duration:.1f} комбинаций/сек")

            logger.info("=" * 70)

            # Выводим статистику
            self.print_statistics()

        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            import traceback
            logger.error(traceback.format_exc())
            raise
        finally:
            self.disconnect()


def main():
    """Точка входа"""
    try:
        # recreate_table=False - НЕ удалять существующую таблицу!
        # Установите True только при первом запуске
        analyzer = CombinationsWinRateAnalyzer(DB_CONFIG, recreate_table=False)
        analyzer.run()
    except KeyboardInterrupt:
        logger.info("\n⛔ Прерывание пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()