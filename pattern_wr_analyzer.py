#!/usr/bin/env python3
"""
Pattern Win Rate Analyzer v2.0
Основан на проверенном analyze_scoring_history.py
Анализирует эффективность паттернов для LONG и SHORT позиций
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
        logging.FileHandler('pattern_wr_analyzer.log'),
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


class PatternWinRateAnalyzer:
    def __init__(self, db_config: dict, recreate_table: bool = False):
        self.db_config = db_config
        self.recreate_table = recreate_table
        self.conn = None
        self.processed_count = 0
        self.error_count = 0
        self.new_patterns_count = 0
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
                    logger.info("🗑️ Удаление старой таблицы fas.test_patterns_wr...")
                    cur.execute("DROP TABLE IF EXISTS fas.test_patterns_wr CASCADE")
                    self.conn.commit()

                # Проверяем существование таблицы
                cur.execute("""
                    SELECT EXISTS (
                        SELECT FROM information_schema.tables 
                        WHERE table_schema = 'fas' 
                        AND table_name = 'test_patterns_wr'
                    )
                """)
                table_exists = cur.fetchone()['exists']

                if not table_exists:
                    # Создаем новую таблицу
                    logger.info("📝 Создание таблицы fas.test_patterns_wr...")
                    create_table_query = """
                    CREATE TABLE fas.test_patterns_wr (
                        id BIGINT PRIMARY KEY,
                        trading_pair_id INTEGER NOT NULL,
                        timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                        pattern_type VARCHAR(100) NOT NULL,
                        timeframe VARCHAR(10) NOT NULL,
                        strength DECIMAL(10,2),
                        confidence DECIMAL(10,2),
                        score_impact DECIMAL(10,2),
                        details JSONB,
                        trigger_values JSONB,

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

                    # Создаем индекс
                    cur.execute("""
                        CREATE INDEX idx_patterns_wr_type 
                        ON fas.test_patterns_wr(pattern_type)
                    """)

                    self.conn.commit()
                    logger.info("✅ Таблица fas.test_patterns_wr создана успешно")
                else:
                    logger.info("✅ Таблица fas.test_patterns_wr уже существует")

        except Exception as e:
            logger.error(f"❌ Ошибка создания таблицы: {e}")
            self.conn.rollback()
            raise

    def get_unprocessed_patterns(self, batch_size: int = 10000) -> List[Dict]:
        """Получение пакета необработанных паттернов старше 48 часов"""
        query = """
            SELECT 
                sp.id,
                sp.trading_pair_id,
                sp.timestamp,
                sp.pattern_type,
                sp.timeframe,
                sp.strength,
                sp.confidence,
                sp.score_impact,
                sp.details,
                sp.trigger_values,
                tp.pair_symbol
            FROM fas.signal_patterns sp
            JOIN public.trading_pairs tp ON sp.trading_pair_id = tp.id
            WHERE sp.timestamp <= NOW() - INTERVAL '48 hours'
                AND NOT EXISTS (
                    SELECT 1 FROM fas.test_patterns_wr tpw
                    WHERE tpw.id = sp.id
                )
            ORDER BY sp.timestamp ASC
            LIMIT %s
        """

        with self.conn.cursor() as cur:
            cur.execute(query, (batch_size,))
            patterns = cur.fetchall()

        return patterns

    def get_total_unprocessed_count(self) -> int:
        """Получение общего количества необработанных паттернов"""
        query = """
            SELECT COUNT(*) as count
            FROM fas.signal_patterns sp
            WHERE sp.timestamp <= NOW() - INTERVAL '48 hours'
                AND NOT EXISTS (
                    SELECT 1 FROM fas.test_patterns_wr tpw
                    WHERE tpw.id = sp.id
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

    def analyze_pattern(self, pattern: Dict) -> Optional[Dict]:
        """Анализ одного паттерна для LONG и SHORT позиций"""
        try:
            # Базовая структура результата (заполним NULL если нет данных)
            base_result = {
                'id': pattern['id'],
                'trading_pair_id': pattern['trading_pair_id'],
                'timestamp': pattern['timestamp'],
                'pattern_type': pattern['pattern_type'],
                'timeframe': pattern['timeframe'],
                'strength': float(pattern['strength']) if pattern['strength'] else None,
                'confidence': float(pattern['confidence']) if pattern['confidence'] else None,
                'score_impact': float(pattern['score_impact']) if pattern['score_impact'] else None,
                'details': pattern['details'],
                'trigger_values': pattern['trigger_values'],
                'sell_tp': False,
                'sell_sl': False,
                'sell_result': None,
                'buy_tp': False,
                'buy_sl': False,
                'buy_result': None
            }

            # Получаем цену входа для LONG
            long_entry_data = self.get_entry_price(
                pattern['trading_pair_id'],
                pattern['timestamp'],
                'LONG'
            )

            # Получаем цену входа для SHORT
            short_entry_data = self.get_entry_price(
                pattern['trading_pair_id'],
                pattern['timestamp'],
                'SHORT'
            )

            if not long_entry_data or not short_entry_data:
                logger.warning(f"⚠️ Нет цены входа для паттерна {pattern['id']}")
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
                    pattern['trading_pair_id'],
                    long_entry_data['entry_time'],
                    long_entry_data['entry_time']
                ))
                history = cur.fetchall()

            if not history or len(history) < 10:
                logger.warning(f"⚠️ Недостаточно истории для паттерна {pattern['id']}")
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

            # Формируем результат
            return {
                'id': pattern['id'],
                'trading_pair_id': pattern['trading_pair_id'],
                'timestamp': pattern['timestamp'],
                'pattern_type': pattern['pattern_type'],
                'timeframe': pattern['timeframe'],
                'strength': float(pattern['strength']) if pattern['strength'] else None,
                'confidence': float(pattern['confidence']) if pattern['confidence'] else None,
                'score_impact': float(pattern['score_impact']) if pattern['score_impact'] else None,
                'details': pattern['details'],
                'trigger_values': pattern['trigger_values'],
                'sell_tp': short_results['tp'],
                'sell_sl': short_results['sl'],
                'sell_result': short_results['result'],
                'buy_tp': long_results['tp'],
                'buy_sl': long_results['sl'],
                'buy_result': long_results['result']
            }

        except Exception as e:
            logger.error(f"❌ Ошибка анализа паттерна {pattern['id']}: {e}")
            self.error_count += 1
            return None

    def save_results(self, results: List[Dict]):
        """Сохранение результатов в БД"""
        if not results:
            return

        insert_query = """
            INSERT INTO fas.test_patterns_wr (
                id, trading_pair_id, timestamp, pattern_type, timeframe,
                strength, confidence, score_impact, details, trigger_values,
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
                        result['trading_pair_id'],
                        result['timestamp'],
                        result['pattern_type'],
                        result['timeframe'],
                        result['strength'],
                        result['confidence'],
                        result['score_impact'],
                        json.dumps(result['details']) if result['details'] else None,
                        json.dumps(result['trigger_values']) if result['trigger_values'] else None,
                        result['sell_tp'],
                        result['sell_sl'],
                        result['sell_result'],
                        result['buy_tp'],
                        result['buy_sl'],
                        result['buy_result']
                    ))
                    saved_count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка сохранения результата для паттерна {result['id']}: {e}")
                    self.error_count += 1

        self.conn.commit()
        self.new_patterns_count += saved_count
        logger.info(f"💾 Сохранено {saved_count} результатов из {len(results)}")

    def print_statistics(self):
        """Вывод статистики по результатам анализа"""
        try:
            stats_query = """
                WITH pattern_stats AS (
                    SELECT 
                        pattern_type,
                        COUNT(*) as total_patterns,
                        COUNT(CASE WHEN buy_result = true THEN 1 END) as long_wins,
                        COUNT(CASE WHEN buy_result = false THEN 1 END) as long_losses,
                        COUNT(CASE WHEN sell_result = true THEN 1 END) as short_wins,
                        COUNT(CASE WHEN sell_result = false THEN 1 END) as short_losses
                    FROM fas.test_patterns_wr
                    WHERE processed_at >= NOW() - INTERVAL '1 day'
                    GROUP BY pattern_type
                    ORDER BY total_patterns DESC
                )
                SELECT * FROM pattern_stats
            """

            with self.conn.cursor() as cur:
                cur.execute(stats_query)
                pattern_stats = cur.fetchall()

            logger.info("\n" + "=" * 70)
            logger.info("📊 СТАТИСТИКА WIN RATE ПО ПАТТЕРНАМ")
            logger.info("=" * 70)

            for stat in pattern_stats:
                pattern = stat['pattern_type']
                total = stat['total_patterns']

                long_total = stat['long_wins'] + stat['long_losses']
                long_wr = (stat['long_wins'] / long_total * 100) if long_total > 0 else 0

                short_total = stat['short_wins'] + stat['short_losses']
                short_wr = (stat['short_wins'] / short_total * 100) if short_total > 0 else 0

                logger.info(f"\n📈 Паттерн: {pattern}")
                logger.info(f"   Всего сигналов: {total}")
                logger.info(f"   ├─ LONG Win Rate: {long_wr:.1f}% ({stat['long_wins']}/{long_total})")
                logger.info(f"   └─ SHORT Win Rate: {short_wr:.1f}% ({stat['short_wins']}/{short_total})")

            logger.info("=" * 70)

        except Exception as e:
            logger.error(f"❌ Ошибка при выводе статистики: {e}")

    def run(self):
        """Основной процесс анализа"""
        start_time = datetime.now()
        logger.info("🚀 Начало анализа паттернов")
        logger.info(f"📅 Время запуска: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"⚙️ Параметры: TP={ANALYSIS_PARAMS['tp_percent']}%, SL={ANALYSIS_PARAMS['sl_percent']}%")

        try:
            self.connect()
            logger.info("📝 Проверка/создание таблицы результатов...")
            self.create_result_table()

            logger.info("🔍 Подсчет необработанных паттернов...")
            total_unprocessed = self.get_total_unprocessed_count()

            if total_unprocessed == 0:
                logger.info("✅ Нет новых паттернов для обработки")
                return

            logger.info(f"📊 Всего необработанных паттернов: {total_unprocessed}")

            batch_size = 10000
            save_batch_size = 100
            batch_number = 0

            while True:
                batch_number += 1
                logger.info(f"\n🔄 Проверка необработанных паттернов...")
                current_unprocessed = self.get_total_unprocessed_count()

                if current_unprocessed == 0:
                    logger.info("✅ Все паттерны обработаны!")
                    break

                logger.info(f"\n📦 Обработка пакета #{batch_number}")
                logger.info(f"📊 Осталось необработанных: {current_unprocessed}")

                logger.info(f"📥 Загрузка пакета паттернов (до {batch_size} штук)...")
                patterns = self.get_unprocessed_patterns(batch_size)

                if not patterns:
                    logger.info(f"✅ Больше нет паттернов для обработки")
                    break

                logger.info(f"📊 В пакете #{batch_number}: {len(patterns)} паттернов")

                # Для отладки - показываем ID первых паттернов
                if len(patterns) <= 30:
                    pattern_ids = [p['id'] for p in patterns]
                    logger.info(f"🔍 ID паттернов в пакете: {pattern_ids}")

                results = []
                batch_processed = 0
                batch_skipped = 0

                for i, pattern in enumerate(patterns):
                    if i % 100 == 0 and i > 0:
                        progress = (i / len(patterns)) * 100
                        logger.info(f"⏳ Пакет #{batch_number}: {i}/{len(patterns)} ({progress:.1f}%)")

                    result = self.analyze_pattern(pattern)
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
                    logger.error(f"❌ Пакет #{batch_number} не содержал обрабатываемых паттернов! Прерывание.")
                    break

                if current_unprocessed > batch_size:
                    time.sleep(2)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info("\n" + "=" * 70)
            logger.info("📋 ИТОГИ ОБРАБОТКИ:")
            logger.info("=" * 70)
            logger.info(f"✅ Успешно обработано: {self.processed_count}")
            logger.info(f"💾 Сохранено новых результатов: {self.new_patterns_count}")
            logger.info(f"⭕ Пропущено: {self.skipped_count}")
            logger.info(f"❌ Ошибок: {self.error_count}")
            logger.info(f"⏱️ Время выполнения: {duration:.1f} секунд ({duration / 60:.1f} минут)")

            if self.processed_count > 0:
                logger.info(f"⚡ Скорость обработки: {self.processed_count / duration:.1f} паттернов/сек")

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
        # Установите recreate_table=True для пересоздания таблицы при первом запуске
        # После первого запуска можно установить False
        analyzer = PatternWinRateAnalyzer(DB_CONFIG, recreate_table=True)
        analyzer.run()
    except KeyboardInterrupt:
        logger.info("\n⛔ Прерывание пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()