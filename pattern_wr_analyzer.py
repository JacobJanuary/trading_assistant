#!/usr/bin/env python3
"""
Pattern Win Rate Analyzer v1.0
Анализирует эффективность торговых паттернов для LONG и SHORT позиций
Оптимизированная версия с векторизацией и параллельной обработкой
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
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, as_completed
import json

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

# Параметры подключения к БД (используйте переменные окружения в production)
DB_CONFIG = {
    'host': os.getenv('DB_HOST', 'localhost'),
    'port': int(os.getenv('DB_PORT', 5432)),
    'dbname': os.getenv('DB_NAME', 'fox_crypto'),
    'user': os.getenv('DB_USER', 'elcrypto'),
    'password': os.getenv('DB_PASSWORD', 'LohNeMamont@!21')
}


# Параметры анализа
@dataclass
class TradingParams:
    tp_percent: float = 4.0
    sl_percent: float = 3.0
    position_size: float = 100.0
    leverage: int = 5
    analysis_hours: int = 48
    entry_delay_minutes: int = 15
    min_candles_required: int = 10
    batch_size: int = 5000
    parallel_workers: int = 4


PARAMS = TradingParams()


class PatternWinRateAnalyzer:
    """Анализатор эффективности паттернов с оптимизированными расчетами"""

    def __init__(self, db_config: dict, params: TradingParams):
        self.db_config = db_config
        self.params = params
        self.conn = None
        self.stats = {
            'processed': 0,
            'errors': 0,
            'no_data': 0,
            'analyzed': 0
        }

    def connect(self):
        """Подключение к БД с оптимальными настройками"""
        try:
            conn_string = " ".join([f"{k}={v}" for k, v in self.db_config.items()])
            self.conn = psycopg.connect(
                conn_string,
                row_factory=dict_row,
                prepare_threshold=10,  # Кэширование prepared statements
                options='-c statement_timeout=30000'  # 30 сек timeout
            )
            logger.info("✅ Успешное подключение к БД")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise

    def disconnect(self):
        """Закрытие соединения"""
        if self.conn:
            self.conn.close()
            logger.info("🔌 Отключение от БД")

    def create_result_table(self):
        """Создание таблицы для хранения результатов анализа"""
        create_table_query = """
        CREATE TABLE IF NOT EXISTS fas.test_patterns_wr (
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
            sell_entry_price DECIMAL(20,8),
            sell_tp_price DECIMAL(20,8),
            sell_sl_price DECIMAL(20,8),
            sell_tp BOOLEAN DEFAULT FALSE,
            sell_sl BOOLEAN DEFAULT FALSE,
            sell_result BOOLEAN,  -- true=TP, false=SL, null=timeout
            sell_close_price DECIMAL(20,8),
            sell_close_time TIMESTAMP WITH TIME ZONE,
            sell_pnl_percent DECIMAL(10,4),
            sell_max_profit_percent DECIMAL(10,4),
            sell_max_drawdown_percent DECIMAL(10,4),

            -- Результаты для LONG
            buy_entry_price DECIMAL(20,8),
            buy_tp_price DECIMAL(20,8),
            buy_sl_price DECIMAL(20,8),
            buy_tp BOOLEAN DEFAULT FALSE,
            buy_sl BOOLEAN DEFAULT FALSE,
            buy_result BOOLEAN,  -- true=TP, false=SL, null=timeout
            buy_close_price DECIMAL(20,8),
            buy_close_time TIMESTAMP WITH TIME ZONE,
            buy_pnl_percent DECIMAL(10,4),
            buy_max_profit_percent DECIMAL(10,4),
            buy_max_drawdown_percent DECIMAL(10,4),

            -- Метаданные
            analysis_completed BOOLEAN DEFAULT FALSE,
            has_sufficient_data BOOLEAN DEFAULT TRUE,
            processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

            -- Индексы для оптимизации
            CONSTRAINT unique_pattern_id UNIQUE(id)
        );

        -- Создаем индексы для ускорения запросов
        CREATE INDEX IF NOT EXISTS idx_patterns_wr_pair_time 
            ON fas.test_patterns_wr(trading_pair_id, timestamp);
        CREATE INDEX IF NOT EXISTS idx_patterns_wr_type 
            ON fas.test_patterns_wr(pattern_type);
        CREATE INDEX IF NOT EXISTS idx_patterns_wr_results 
            ON fas.test_patterns_wr(sell_result, buy_result);
        """

        try:
            with self.conn.cursor() as cur:
                cur.execute(create_table_query)
            self.conn.commit()
            logger.info("✅ Таблица fas.test_patterns_wr создана/проверена")
        except Exception as e:
            logger.error(f"❌ Ошибка создания таблицы: {e}")
            raise

    def get_unprocessed_patterns(self, limit: int = None) -> List[Dict]:
        """Получение необработанных паттернов старше 48 часов"""
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
            tp.pair_symbol as pair_symbol
        FROM fas.signal_patterns sp
        JOIN public.trading_pairs tp ON sp.trading_pair_id = tp.id
        WHERE sp.timestamp <= NOW() - INTERVAL '48 hours'
            AND NOT EXISTS (
                SELECT 1 FROM fas.test_patterns_wr tpw
                WHERE tpw.id = sp.id AND tpw.analysis_completed = TRUE
            )
        ORDER BY sp.timestamp ASC
        """

        if limit:
            query += f" LIMIT {limit}"

        with self.conn.cursor() as cur:
            cur.execute(query)
            patterns = cur.fetchall()

        logger.info(f"📊 Найдено {len(patterns)} необработанных паттернов")
        return patterns

    def get_price_data_vectorized(self, trading_pair_id: int,
                                  start_time: datetime,
                                  end_time: datetime) -> Optional[np.ndarray]:
        """Получение ценовых данных в виде numpy массива для векторизованных расчетов"""
        query = """
        SELECT 
            EXTRACT(EPOCH FROM timestamp) as timestamp_epoch,
            close_price::FLOAT,
            high_price::FLOAT,
            low_price::FLOAT,
            open_price::FLOAT
        FROM fas.market_data_aggregated
        WHERE trading_pair_id = %s
            AND timeframe = '15m'
            AND timestamp >= %s
            AND timestamp <= %s
        ORDER BY timestamp ASC
        """

        try:
            with self.conn.cursor() as cur:
                cur.execute(query, (trading_pair_id, start_time, end_time))
                data = cur.fetchall()

            if len(data) < self.params.min_candles_required:
                return None

            # Конвертируем в numpy массив для быстрых расчетов
            return np.array([
                (row['timestamp_epoch'], row['close_price'],
                 row['high_price'], row['low_price'], row['open_price'])
                for row in data
            ], dtype=[
                ('timestamp', 'f8'),
                ('close', 'f8'),
                ('high', 'f8'),
                ('low', 'f8'),
                ('open', 'f8')
            ])

        except Exception as e:
            logger.error(f"❌ Ошибка получения ценовых данных: {e}")
            return None

    def analyze_position_vectorized(self, signal_type: str, entry_price: float,
                                    price_data: np.ndarray) -> Dict:
        """
        Векторизованный анализ позиции для максимальной производительности
        Использует numpy для одновременной обработки всех свечей
        """
        tp_pct = self.params.tp_percent / 100
        sl_pct = self.params.sl_percent / 100

        if signal_type == 'LONG':
            tp_price = entry_price * (1 + tp_pct)
            sl_price = entry_price * (1 - sl_pct)

            # Векторизованная проверка TP/SL
            tp_hits = price_data['high'] >= tp_price
            sl_hits = price_data['low'] <= sl_price

            # Расчет максимального потенциала
            max_price = np.max(price_data['high'])
            min_price = np.min(price_data['low'])
            max_profit_pct = ((max_price - entry_price) / entry_price) * 100
            max_drawdown_pct = ((entry_price - min_price) / entry_price) * 100

        else:  # SHORT
            tp_price = entry_price * (1 - tp_pct)
            sl_price = entry_price * (1 + sl_pct)

            tp_hits = price_data['low'] <= tp_price
            sl_hits = price_data['high'] >= sl_price

            max_price = np.max(price_data['high'])
            min_price = np.min(price_data['low'])
            max_profit_pct = ((entry_price - min_price) / entry_price) * 100
            max_drawdown_pct = ((max_price - entry_price) / entry_price) * 100

        # Определяем первое срабатывание
        result = None
        close_price = None
        close_time = None
        close_idx = None

        # Приоритет: сначала проверяем SL (защита капитала)
        for i in range(len(price_data)):
            if sl_hits[i]:
                result = False  # SL сработал
                close_price = sl_price
                close_idx = i
                break
            elif tp_hits[i]:
                result = True  # TP сработал
                close_price = tp_price
                close_idx = i
                break

        if close_idx is not None:
            close_time = datetime.fromtimestamp(
                price_data['timestamp'][close_idx],
                tz=timezone.utc
            )
        else:
            # Timeout - закрытие по последней цене
            close_price = price_data['close'][-1]
            close_time = datetime.fromtimestamp(
                price_data['timestamp'][-1],
                tz=timezone.utc
            )

        # Расчет финального P&L
        if signal_type == 'LONG':
            pnl_percent = ((close_price - entry_price) / entry_price) * 100
        else:  # SHORT
            pnl_percent = ((entry_price - close_price) / entry_price) * 100

        return {
            'entry_price': entry_price,
            'tp_price': tp_price,
            'sl_price': sl_price,
            'tp': result is True,
            'sl': result is False,
            'result': result,
            'close_price': close_price,
            'close_time': close_time,
            'pnl_percent': pnl_percent,
            'max_profit_percent': max_profit_pct,
            'max_drawdown_percent': max_drawdown_pct
        }

    def analyze_pattern(self, pattern: Dict) -> Optional[Dict]:
        """Анализ одного паттерна для LONG и SHORT позиций"""
        try:
            # Время входа с задержкой
            entry_time = pattern['timestamp'] + timedelta(minutes=self.params.entry_delay_minutes)
            analysis_end = entry_time + timedelta(hours=self.params.analysis_hours)

            # Получаем ценовые данные
            price_data = self.get_price_data_vectorized(
                pattern['trading_pair_id'],
                entry_time,
                analysis_end
            )

            if price_data is None or len(price_data) == 0:
                logger.warning(f"⚠️ Недостаточно данных для паттерна {pattern['id']}")
                self.stats['no_data'] += 1
                return self._create_no_data_result(pattern)

            # Цена входа - используем худший сценарий
            # Для LONG - high первой свечи, для SHORT - low первой свечи
            long_entry = price_data['high'][0]
            short_entry = price_data['low'][0]

            # Анализируем LONG позицию
            long_results = self.analyze_position_vectorized(
                'LONG', long_entry, price_data[1:]  # Skip первую свечу
            )

            # Анализируем SHORT позицию
            short_results = self.analyze_position_vectorized(
                'SHORT', short_entry, price_data[1:]
            )

            # Формируем результат
            result = {
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

                # SHORT результаты
                'sell_entry_price': short_results['entry_price'],
                'sell_tp_price': short_results['tp_price'],
                'sell_sl_price': short_results['sl_price'],
                'sell_tp': short_results['tp'],
                'sell_sl': short_results['sl'],
                'sell_result': short_results['result'],
                'sell_close_price': short_results['close_price'],
                'sell_close_time': short_results['close_time'],
                'sell_pnl_percent': short_results['pnl_percent'],
                'sell_max_profit_percent': short_results['max_profit_percent'],
                'sell_max_drawdown_percent': short_results['max_drawdown_percent'],

                # LONG результаты
                'buy_entry_price': long_results['entry_price'],
                'buy_tp_price': long_results['tp_price'],
                'buy_sl_price': long_results['sl_price'],
                'buy_tp': long_results['tp'],
                'buy_sl': long_results['sl'],
                'buy_result': long_results['result'],
                'buy_close_price': long_results['close_price'],
                'buy_close_time': long_results['close_time'],
                'buy_pnl_percent': long_results['pnl_percent'],
                'buy_max_profit_percent': long_results['max_profit_percent'],
                'buy_max_drawdown_percent': long_results['max_drawdown_percent'],

                'analysis_completed': True,
                'has_sufficient_data': True
            }

            self.stats['analyzed'] += 1
            return result

        except Exception as e:
            logger.error(f"❌ Ошибка анализа паттерна {pattern['id']}: {e}")
            self.stats['errors'] += 1
            return None

    def _create_no_data_result(self, pattern: Dict) -> Dict:
        """Создание записи для паттернов без достаточных данных"""
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
            'analysis_completed': True,
            'has_sufficient_data': False
        }

    def save_results_batch(self, results: List[Dict]):
        """Пакетное сохранение результатов с использованием COPY для скорости"""
        if not results:
            return

        insert_query = """
        INSERT INTO fas.test_patterns_wr (
            id, trading_pair_id, timestamp, pattern_type, timeframe,
            strength, confidence, score_impact, details, trigger_values,
            sell_entry_price, sell_tp_price, sell_sl_price,
            sell_tp, sell_sl, sell_result, sell_close_price, sell_close_time,
            sell_pnl_percent, sell_max_profit_percent, sell_max_drawdown_percent,
            buy_entry_price, buy_tp_price, buy_sl_price,
            buy_tp, buy_sl, buy_result, buy_close_price, buy_close_time,
            buy_pnl_percent, buy_max_profit_percent, buy_max_drawdown_percent,
            analysis_completed, has_sufficient_data
        ) VALUES %s
        ON CONFLICT (id) DO UPDATE SET
            sell_result = EXCLUDED.sell_result,
            buy_result = EXCLUDED.buy_result,
            sell_pnl_percent = EXCLUDED.sell_pnl_percent,
            buy_pnl_percent = EXCLUDED.buy_pnl_percent,
            processed_at = NOW()
        """

        try:
            with self.conn.cursor() as cur:
                # Подготавливаем данные для вставки
                values = []
                for r in results:
                    values.append((
                        r['id'], r['trading_pair_id'], r['timestamp'],
                        r['pattern_type'], r['timeframe'],
                        r.get('strength'), r.get('confidence'), r.get('score_impact'),
                        json.dumps(r['details']) if r['details'] else None,
                        json.dumps(r['trigger_values']) if r['trigger_values'] else None,
                        r.get('sell_entry_price'), r.get('sell_tp_price'), r.get('sell_sl_price'),
                        r.get('sell_tp', False), r.get('sell_sl', False), r.get('sell_result'),
                        r.get('sell_close_price'), r.get('sell_close_time'),
                        r.get('sell_pnl_percent'), r.get('sell_max_profit_percent'),
                        r.get('sell_max_drawdown_percent'),
                        r.get('buy_entry_price'), r.get('buy_tp_price'), r.get('buy_sl_price'),
                        r.get('buy_tp', False), r.get('buy_sl', False), r.get('buy_result'),
                        r.get('buy_close_price'), r.get('buy_close_time'),
                        r.get('buy_pnl_percent'), r.get('buy_max_profit_percent'),
                        r.get('buy_max_drawdown_percent'),
                        r.get('analysis_completed', False), r.get('has_sufficient_data', True)
                    ))

                # Используем execute_values для быстрой вставки
                from psycopg.extras import execute_values
                execute_values(cur, insert_query, values)

            self.conn.commit()
            logger.info(f"💾 Сохранено {len(results)} результатов")

        except Exception as e:
            logger.error(f"❌ Ошибка сохранения результатов: {e}")
            self.conn.rollback()
            raise

    def process_patterns_parallel(self, patterns: List[Dict]):
        """Параллельная обработка паттернов для ускорения"""
        results = []

        with ThreadPoolExecutor(max_workers=self.params.parallel_workers) as executor:
            # Создаем задачи
            future_to_pattern = {
                executor.submit(self.analyze_pattern, pattern): pattern
                for pattern in patterns
            }

            # Обрабатываем результаты по мере готовности
            for future in as_completed(future_to_pattern):
                pattern = future_to_pattern[future]
                try:
                    result = future.result(timeout=30)
                    if result:
                        results.append(result)
                        self.stats['processed'] += 1

                    # Сохраняем батчами
                    if len(results) >= 100:
                        self.save_results_batch(results)
                        results = []

                except Exception as e:
                    logger.error(f"❌ Ошибка обработки паттерна {pattern['id']}: {e}")
                    self.stats['errors'] += 1

        # Сохраняем остатки
        if results:
            self.save_results_batch(results)

    def print_statistics(self):
        """Вывод детальной статистики по результатам анализа"""
        stats_query = """
        WITH pattern_stats AS (
            SELECT 
                pattern_type,
                COUNT(*) as total_patterns,

                -- LONG статистика
                COUNT(CASE WHEN buy_result = true THEN 1 END) as long_wins,
                COUNT(CASE WHEN buy_result = false THEN 1 END) as long_losses,
                COUNT(CASE WHEN buy_result IS NULL AND has_sufficient_data THEN 1 END) as long_timeouts,
                AVG(CASE WHEN buy_result IS NOT NULL THEN buy_pnl_percent END) as avg_long_pnl,
                MAX(buy_pnl_percent) as max_long_profit,
                MIN(buy_pnl_percent) as max_long_loss,
                AVG(buy_max_profit_percent) as avg_long_potential,

                -- SHORT статистика
                COUNT(CASE WHEN sell_result = true THEN 1 END) as short_wins,
                COUNT(CASE WHEN sell_result = false THEN 1 END) as short_losses,
                COUNT(CASE WHEN sell_result IS NULL AND has_sufficient_data THEN 1 END) as short_timeouts,
                AVG(CASE WHEN sell_result IS NOT NULL THEN sell_pnl_percent END) as avg_short_pnl,
                MAX(sell_pnl_percent) as max_short_profit,
                MIN(sell_pnl_percent) as max_short_loss,
                AVG(sell_max_profit_percent) as avg_short_potential,

                COUNT(CASE WHEN NOT has_sufficient_data THEN 1 END) as no_data_count

            FROM fas.test_patterns_wr
            WHERE processed_at >= NOW() - INTERVAL '1 day'
            GROUP BY pattern_type
            ORDER BY total_patterns DESC
        ),
        overall_stats AS (
            SELECT 
                COUNT(*) as total_analyzed,
                COUNT(CASE WHEN buy_result = true THEN 1 END) as total_long_wins,
                COUNT(CASE WHEN buy_result = false THEN 1 END) as total_long_losses,
                COUNT(CASE WHEN sell_result = true THEN 1 END) as total_short_wins,
                COUNT(CASE WHEN sell_result = false THEN 1 END) as total_short_losses,
                AVG(buy_pnl_percent) as overall_avg_long_pnl,
                AVG(sell_pnl_percent) as overall_avg_short_pnl
            FROM fas.test_patterns_wr
            WHERE processed_at >= NOW() - INTERVAL '1 day'
                AND has_sufficient_data = true
        )
        SELECT * FROM pattern_stats
        """

        try:
            with self.conn.cursor() as cur:
                cur.execute(stats_query)
                pattern_stats = cur.fetchall()

            logger.info("\n" + "=" * 80)
            logger.info("📊 СТАТИСТИКА WIN RATE ПО ПАТТЕРНАМ")
            logger.info("=" * 80)

            for stat in pattern_stats:
                pattern = stat['pattern_type']
                total = stat['total_patterns']

                # LONG Win Rate
                long_total = stat['long_wins'] + stat['long_losses']
                long_wr = (stat['long_wins'] / long_total * 100) if long_total > 0 else 0

                # SHORT Win Rate
                short_total = stat['short_wins'] + stat['short_losses']
                short_wr = (stat['short_wins'] / short_total * 100) if short_total > 0 else 0

                logger.info(f"\n📈 Паттерн: {pattern}")
                logger.info(f"   Всего сигналов: {total}")
                logger.info(f"   ├─ LONG Win Rate: {long_wr:.1f}% ({stat['long_wins']}/{long_total})")
                logger.info(f"   │  ├─ Avg P&L: {stat['avg_long_pnl']:.2f}%" if stat[
                    'avg_long_pnl'] else "   │  ├─ Avg P&L: N/A")
                logger.info(f"   │  └─ Max потенциал: {stat['avg_long_potential']:.2f}%" if stat[
                    'avg_long_potential'] else "   │  └─ Max потенциал: N/A")
                logger.info(f"   └─ SHORT Win Rate: {short_wr:.1f}% ({stat['short_wins']}/{short_total})")
                logger.info(f"      ├─ Avg P&L: {stat['avg_short_pnl']:.2f}%" if stat[
                    'avg_short_pnl'] else "      ├─ Avg P&L: N/A")
                logger.info(f"      └─ Max потенциал: {stat['avg_short_potential']:.2f}%" if stat[
                    'avg_short_potential'] else "      └─ Max потенциал: N/A")

                if stat['no_data_count'] > 0:
                    logger.info(f"   ⚠️ Без данных: {stat['no_data_count']}")

            # Общая статистика
            cur.execute("SELECT * FROM overall_stats")
            overall = cur.fetchone()

            if overall:
                logger.info("\n" + "=" * 80)
                logger.info("🎯 ОБЩАЯ СТАТИСТИКА")
                logger.info("=" * 80)

                total_long = overall['total_long_wins'] + overall['total_long_losses']
                total_short = overall['total_short_wins'] + overall['total_short_losses']

                if total_long > 0:
                    long_wr = (overall['total_long_wins'] / total_long) * 100
                    logger.info(f"LONG:  WR={long_wr:.1f}%, Avg P&L={overall['overall_avg_long_pnl']:.2f}%")

                if total_short > 0:
                    short_wr = (overall['total_short_wins'] / total_short) * 100
                    logger.info(f"SHORT: WR={short_wr:.1f}%, Avg P&L={overall['overall_avg_short_pnl']:.2f}%")

            logger.info("=" * 80)

        except Exception as e:
            logger.error(f"❌ Ошибка вывода статистики: {e}")

    def run(self):
        """Основной процесс анализа"""
        start_time = datetime.now()
        logger.info("🚀 Запуск Pattern Win Rate Analyzer v1.0")
        logger.info(f"📅 Время запуска: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info(f"⚙️ Параметры: TP={self.params.tp_percent}%, SL={self.params.sl_percent}%")
        logger.info(f"🔧 Параллельных воркеров: {self.params.parallel_workers}")

        try:
            self.connect()
            self.create_result_table()

            # Получаем все необработанные паттерны
            patterns = self.get_unprocessed_patterns()

            if not patterns:
                logger.info("✅ Нет новых паттернов для обработки")
                return

            logger.info(f"📊 Начинаем обработку {len(patterns)} паттернов")

            # Обрабатываем батчами с параллельной обработкой
            batch_size = self.params.batch_size
            for i in range(0, len(patterns), batch_size):
                batch = patterns[i:i + batch_size]
                batch_num = i // batch_size + 1
                total_batches = (len(patterns) + batch_size - 1) // batch_size

                logger.info(f"\n📦 Обработка батча {batch_num}/{total_batches} ({len(batch)} паттернов)")
                self.process_patterns_parallel(batch)

                # Прогресс
                progress = min(100, ((i + len(batch)) / len(patterns)) * 100)
                logger.info(f"⏳ Общий прогресс: {progress:.1f}%")

            # Финальная статистика
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info("\n" + "=" * 80)
            logger.info("📋 ИТОГИ ОБРАБОТКИ")
            logger.info("=" * 80)
            logger.info(f"✅ Обработано: {self.stats['processed']}")
            logger.info(f"📊 Проанализировано: {self.stats['analyzed']}")
            logger.info(f"⚠️ Без данных: {self.stats['no_data']}")
            logger.info(f"❌ Ошибок: {self.stats['errors']}")
            logger.info(f"⏱️ Время выполнения: {duration:.1f} сек ({duration / 60:.1f} мин)")

            if self.stats['processed'] > 0:
                logger.info(f"⚡ Скорость: {self.stats['processed'] / duration:.1f} паттернов/сек")

            # Выводим детальную статистику
            self.print_statistics()

        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            raise
        finally:
            self.disconnect()


def main():
    """Точка входа"""
    try:
        analyzer = PatternWinRateAnalyzer(DB_CONFIG, PARAMS)
        analyzer.run()
    except KeyboardInterrupt:
        logger.info("\n⛔ Прерывание пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()