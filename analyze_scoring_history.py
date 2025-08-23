#!/usr/bin/env python3
"""
Скрипт для анализа исторических данных скоринга
Анализирует все сигналы старше 48 часов и сохраняет результаты в БД
Version: 3.0 - Исправлен расчет максимального потенциала
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
        logging.FileHandler('analyze_scoring_history.log'),
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
    'analysis_hours': 48,  # Анализируем 48 часов после сигнала
    'entry_delay_minutes': 15  # Задержка входа после сигнала
}


class ScoringAnalyzer:
    def __init__(self, db_config: dict):
        self.db_config = db_config
        self.conn = None
        self.processed_count = 0
        self.error_count = 0
        self.new_signals_count = 0
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

    def get_unprocessed_signals(self, batch_size: int = 10000) -> List[Dict]:
        """Получение пакета необработанных сигналов старше 48 часов"""
        query = """
            SELECT 
                sh.id as scoring_history_id,
                sh.timestamp as signal_timestamp,
                sh.trading_pair_id,
                sh.pair_symbol,
                sh.total_score,
                sh.indicator_score,
                sh.pattern_score,
                sh.combination_score,
                mr.regime as market_regime
            FROM fas.scoring_history sh
            LEFT JOIN LATERAL (
                SELECT regime
                FROM fas.market_regime mr
                WHERE mr.timestamp <= sh.timestamp
                    AND mr.timeframe = '4h'
                ORDER BY mr.timestamp DESC
                LIMIT 1
            ) mr ON true
            WHERE sh.timestamp <= NOW() - INTERVAL '48 hours'
                AND NOT EXISTS (
                    SELECT 1 FROM web.scoring_history_results shr
                    WHERE shr.scoring_history_id = sh.id
                )
            ORDER BY sh.timestamp ASC
            LIMIT %s
        """

        with self.conn.cursor() as cur:
            cur.execute(query, (batch_size,))
            signals = cur.fetchall()

        return signals

    def get_total_unprocessed_count(self) -> int:
        """Получение общего количества необработанных сигналов"""
        query = """
            SELECT COUNT(*) as count
            FROM fas.scoring_history sh
            WHERE sh.timestamp <= NOW() - INTERVAL '48 hours'
                AND NOT EXISTS (
                    SELECT 1 FROM web.scoring_history_results shr
                    WHERE shr.scoring_history_id = sh.id
                )
        """

        with self.conn.cursor() as cur:
            cur.execute(query)
            result = cur.fetchone()
            return result['count'] if result else 0

    def determine_signal_type(self, signal: Dict) -> Tuple[str, str]:
        """Определение типа сигнала на основе indicator_score"""
        indicator_score = float(signal.get('indicator_score', 0))

        if indicator_score >= 0:
            return 'BUY', f"Indicator Score: {indicator_score:.1f}"
        else:
            return 'SELL', f"Indicator Score: {indicator_score:.1f}"

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
                # Для BUY используем high_price (худший вход), для SELL - low_price
                if signal_type == 'BUY':
                    entry_price = float(result['high_price'])
                else:  # SELL
                    entry_price = float(result['low_price'])

                return {
                    'entry_price': entry_price,
                    'entry_time': result['timestamp']
                }
            return None

        except Exception as e:
            logger.error(f"❌ Ошибка получения цены входа: {e}")
            return None

    def process_price_history_improved(self, signal_type: str, entry_price: float,
                                       history: List[Dict], actual_entry_time) -> Dict:
        """
        ИСПРАВЛЕННАЯ версия обработки истории цен
        Корректно рассчитывает максимальный потенциал за ВСЕ 48 часов
        """
        tp_percent = ANALYSIS_PARAMS['tp_percent']
        sl_percent = ANALYSIS_PARAMS['sl_percent']
        position_size = ANALYSIS_PARAMS['position_size']
        leverage = ANALYSIS_PARAMS['leverage']

        # Расчет уровней TP и SL
        if signal_type == 'BUY':
            tp_price = entry_price * (1 + tp_percent / 100)
            sl_price = entry_price * (1 - sl_percent / 100)
        else:  # SELL
            tp_price = entry_price * (1 - tp_percent / 100)
            sl_price = entry_price * (1 + sl_percent / 100)

        # Переменные для отслеживания реальной торговли
        is_closed = False
        close_reason = None
        close_price = None
        close_time = None
        hours_to_close = None
        is_win = None

        # Переменные для отслеживания АБСОЛЮТНЫХ экстремумов (за все 48 часов)
        absolute_max_price = entry_price
        absolute_min_price = entry_price
        time_to_max = 0
        time_to_min = 0

        # Переменные для трекинга максимальной просадки от лучшей точки
        running_best_price = entry_price  # Лучшая цена на текущий момент
        max_drawdown_from_peak = 0  # Максимальная просадка от пика

        # Сначала проходим ВСЕ свечи для нахождения экстремумов
        logger.debug(f"Анализ {len(history)} свечей для {signal_type} с entry_price={entry_price:.2f}")

        for i, candle in enumerate(history):
            current_time = candle['timestamp']
            hours_passed = (current_time - actual_entry_time).total_seconds() / 3600

            high_price = float(candle['high_price'])
            low_price = float(candle['low_price'])

            # Обновляем АБСОЛЮТНЫЕ экстремумы (для всего периода)
            if high_price > absolute_max_price:
                absolute_max_price = high_price
                time_to_max = hours_passed

            if low_price < absolute_min_price:
                absolute_min_price = low_price
                time_to_min = hours_passed

            # Проверяем закрытие позиции (только если еще не закрыта)
            if not is_closed:
                if signal_type == 'BUY':
                    # Сначала проверяем SL (приоритет защиты капитала)
                    if low_price <= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    # Затем проверяем TP
                    elif high_price >= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

                else:  # SELL
                    # Сначала проверяем SL
                    if high_price >= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    # Затем проверяем TP
                    elif low_price <= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

            # Обновляем running best и считаем просадку от него
            if signal_type == 'BUY':
                if high_price > running_best_price:
                    running_best_price = high_price
                # Просадка от лучшей точки
                current_drawdown = ((running_best_price - low_price) / running_best_price) * 100
                if current_drawdown > max_drawdown_from_peak:
                    max_drawdown_from_peak = current_drawdown
            else:  # SELL
                if low_price < running_best_price:
                    running_best_price = low_price
                # Просадка от лучшей точки для SHORT
                current_drawdown = ((high_price - running_best_price) / running_best_price) * 100
                if current_drawdown > max_drawdown_from_peak:
                    max_drawdown_from_peak = current_drawdown

        # Если не закрылась за 48 часов
        if not is_closed:
            is_closed = True
            close_reason = 'timeout'
            is_win = None
            close_price = float(history[-1]['close_price'])
            close_time = history[-1]['timestamp']
            hours_to_close = 48.0

        # ИСПРАВЛЕННЫЙ расчет максимального потенциала (на основе ВСЕХ данных за 48 часов)
        if signal_type == 'BUY':
            # Максимальный возможный профит (если бы продали на абсолютном максимуме)
            max_potential_profit_percent = ((absolute_max_price - entry_price) / entry_price) * 100
            max_potential_profit_usd = position_size * leverage * (max_potential_profit_percent / 100)

            # Максимальный возможный убыток (если бы продали на абсолютном минимуме)
            max_potential_loss_percent = ((entry_price - absolute_min_price) / entry_price) * 100
            max_potential_loss_usd = position_size * leverage * (max_potential_loss_percent / 100)

            # Фактический P&L
            final_pnl_percent = ((close_price - entry_price) / entry_price) * 100

        else:  # SELL
            # Максимальный возможный профит для SHORT (если бы закрыли на минимуме)
            max_potential_profit_percent = ((entry_price - absolute_min_price) / entry_price) * 100
            max_potential_profit_usd = position_size * leverage * (max_potential_profit_percent / 100)

            # Максимальный возможный убыток для SHORT (если бы закрыли на максимуме)
            max_potential_loss_percent = ((absolute_max_price - entry_price) / entry_price) * 100
            max_potential_loss_usd = position_size * leverage * (max_potential_loss_percent / 100)

            # Фактический P&L
            final_pnl_percent = ((entry_price - close_price) / entry_price) * 100

        final_pnl_usd = position_size * leverage * (final_pnl_percent / 100)

        # Дополнительная статистика
        price_range_percent = ((absolute_max_price - absolute_min_price) / entry_price) * 100

        logger.debug(f"Результаты анализа: "
                     f"close_reason={close_reason}, "
                     f"pnl={final_pnl_percent:.2f}%, "
                     f"max_potential_profit={max_potential_profit_percent:.2f}%, "
                     f"max_potential_loss={max_potential_loss_percent:.2f}%")

        return {
            'best_price': absolute_max_price if signal_type == 'BUY' else absolute_min_price,
            'worst_price': absolute_min_price if signal_type == 'BUY' else absolute_max_price,
            'close_price': close_price,
            'is_closed': is_closed,
            'close_reason': close_reason,
            'is_win': is_win,
            'close_time': close_time,
            'hours_to_close': hours_to_close,
            'pnl_percent': final_pnl_percent,
            'pnl_usd': final_pnl_usd,
            'max_potential_profit_percent': max_potential_profit_percent,
            'max_potential_profit_usd': max_potential_profit_usd,
            'max_drawdown_percent': max_potential_loss_percent,  # Максимальный возможный убыток
            'max_drawdown_usd': max_potential_loss_usd,
            'tp_percent': tp_percent,
            'sl_percent': sl_percent,
            'position_size': position_size,
            'leverage': leverage,
            'analysis_end_time': actual_entry_time + timedelta(hours=48),
            # Дополнительные метрики
            'absolute_max_price': absolute_max_price,
            'absolute_min_price': absolute_min_price,
            'time_to_max_hours': time_to_max,
            'time_to_min_hours': time_to_min,
            'price_range_percent': price_range_percent,
            'max_drawdown_from_peak_percent': max_drawdown_from_peak
        }

    def analyze_signal(self, signal: Dict) -> Optional[Dict]:
        """Анализ одного сигнала с использованием исправленной логики"""
        try:
            # Определяем тип сигнала
            signal_type, signal_criteria = self.determine_signal_type(signal)

            # Получаем цену входа с учетом 15-минутной задержки
            entry_data = self.get_entry_price(
                signal['trading_pair_id'],
                signal['signal_timestamp'],
                signal_type
            )

            if not entry_data:
                logger.warning(f"⚠️ Нет цены входа для {signal['pair_symbol']} @ {signal['signal_timestamp']}")
                self.skipped_count += 1
                return None

            entry_price = entry_data['entry_price']
            actual_entry_time = entry_data['entry_time']

            # Получаем историю цен из market_data_aggregated за 48 часов
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
                    signal['trading_pair_id'],
                    actual_entry_time,
                    actual_entry_time
                ))
                history = cur.fetchall()

            if not history or len(history) < 10:  # Минимум 10 свечей для анализа
                logger.warning(f"⚠️ Недостаточно истории для {signal['pair_symbol']}")
                self.skipped_count += 1
                return None

            # Используем ИСПРАВЛЕННЫЙ метод анализа
            result = self.process_price_history_improved(
                signal_type,
                entry_price,
                history,
                actual_entry_time
            )

            # Формируем результат
            return {
                'scoring_history_id': signal['scoring_history_id'],
                'signal_timestamp': signal['signal_timestamp'],
                'pair_symbol': signal['pair_symbol'],
                'trading_pair_id': signal['trading_pair_id'],
                'market_regime': signal['market_regime'],
                'total_score': float(signal['total_score']),
                'indicator_score': float(signal['indicator_score']),
                'pattern_score': float(signal['pattern_score']),
                'combination_score': float(signal.get('combination_score', 0)),
                'signal_type': signal_type,
                'signal_criteria': signal_criteria,
                'entry_price': entry_price,
                **result
            }

        except Exception as e:
            logger.error(f"❌ Ошибка анализа сигнала {signal['pair_symbol']}: {e}")
            self.error_count += 1
            return None

    def save_results(self, results: List[Dict]):
        """Сохранение результатов в БД"""
        if not results:
            return

        insert_query = """
            INSERT INTO web.scoring_history_results (
                scoring_history_id, signal_timestamp, pair_symbol, trading_pair_id,
                market_regime, total_score, indicator_score, pattern_score, combination_score,
                signal_type, signal_criteria,
                entry_price, best_price, worst_price, close_price,
                is_closed, close_reason, is_win, close_time, hours_to_close,
                pnl_percent, pnl_usd,
                max_potential_profit_percent, max_potential_profit_usd,
                max_drawdown_percent, max_drawdown_usd,
                tp_percent, sl_percent, position_size, leverage,
                analysis_end_time
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (scoring_history_id) DO UPDATE SET
                max_potential_profit_percent = EXCLUDED.max_potential_profit_percent,
                max_potential_profit_usd = EXCLUDED.max_potential_profit_usd,
                max_drawdown_percent = EXCLUDED.max_drawdown_percent,
                max_drawdown_usd = EXCLUDED.max_drawdown_usd,
                processed_at = NOW()
        """

        saved_count = 0
        with self.conn.cursor() as cur:
            for result in results:
                try:
                    cur.execute(insert_query, (
                        result['scoring_history_id'],
                        result['signal_timestamp'],
                        result['pair_symbol'],
                        result['trading_pair_id'],
                        result['market_regime'],
                        result['total_score'],
                        result['indicator_score'],
                        result['pattern_score'],
                        result['combination_score'],
                        result['signal_type'],
                        result['signal_criteria'],
                        result['entry_price'],
                        result['best_price'],
                        result['worst_price'],
                        result['close_price'],
                        result['is_closed'],
                        result['close_reason'],
                        result['is_win'],
                        result['close_time'],
                        result['hours_to_close'],
                        result['pnl_percent'],
                        result['pnl_usd'],
                        result['max_potential_profit_percent'],
                        result['max_potential_profit_usd'],
                        result['max_drawdown_percent'],
                        result['max_drawdown_usd'],
                        result['tp_percent'],
                        result['sl_percent'],
                        result['position_size'],
                        result['leverage'],
                        result['analysis_end_time']
                    ))
                    saved_count += 1
                except Exception as e:
                    logger.error(f"❌ Ошибка сохранения результата для {result['pair_symbol']}: {e}")
                    self.error_count += 1

        self.conn.commit()
        self.new_signals_count += saved_count
        logger.info(f"💾 Сохранено {saved_count} результатов из {len(results)}")

    def print_enhanced_statistics(self):
        """Вывод расширенной статистики с анализом максимального потенциала"""
        try:
            stats_query = """
                WITH stats AS (
                    SELECT 
                        COUNT(*) as total_signals,
                        COUNT(CASE WHEN signal_type = 'BUY' THEN 1 END) as buy_signals,
                        COUNT(CASE WHEN signal_type = 'SELL' THEN 1 END) as sell_signals,
                        COUNT(CASE WHEN is_win = true THEN 1 END) as wins,
                        COUNT(CASE WHEN is_win = false THEN 1 END) as losses,
                        COUNT(CASE WHEN is_win IS NULL THEN 1 END) as timeouts,
                        AVG(pnl_usd) as avg_pnl,
                        SUM(pnl_usd) as total_pnl,
                        AVG(CASE WHEN is_win = true THEN pnl_usd END) as avg_win_profit,
                        AVG(CASE WHEN is_win = false THEN pnl_usd END) as avg_loss,
                        MAX(pnl_usd) as max_profit,
                        MIN(pnl_usd) as max_loss,
                        AVG(max_potential_profit_usd) as avg_max_potential_profit,
                        AVG(max_drawdown_usd) as avg_max_potential_loss,
                        AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close,
                        -- Новые метрики
                        AVG(max_potential_profit_percent) as avg_max_potential_profit_pct,
                        AVG(max_drawdown_percent) as avg_max_potential_loss_pct,
                        COUNT(CASE WHEN max_potential_profit_usd > pnl_usd AND pnl_usd > 0 THEN 1 END) as missed_profit_count,
                        AVG(CASE WHEN max_potential_profit_usd > pnl_usd AND pnl_usd > 0 
                            THEN max_potential_profit_usd - pnl_usd END) as avg_missed_profit
                    FROM web.scoring_history_results
                    WHERE processed_at >= NOW() - INTERVAL '1 day'
                )
                SELECT * FROM stats
            """

            with self.conn.cursor() as cur:
                cur.execute(stats_query)
                stats = cur.fetchone()

            logger.info("=" * 70)
            logger.info("📊 РАСШИРЕННАЯ СТАТИСТИКА ЗА ПОСЛЕДНИЕ 24 ЧАСА:")
            logger.info("=" * 70)

            if stats and stats['total_signals'] > 0:
                logger.info(f"📈 Всего сигналов: {stats['total_signals']}")
                logger.info(
                    f"   ├─ BUY: {stats['buy_signals']} ({stats['buy_signals'] / stats['total_signals'] * 100:.1f}%)")
                logger.info(
                    f"   └─ SELL: {stats['sell_signals']} ({stats['sell_signals'] / stats['total_signals'] * 100:.1f}%)")

                logger.info(f"\n🎯 Результаты торговли:")
                logger.info(f"   ├─ Победы (TP): {stats['wins']}")
                logger.info(f"   ├─ Поражения (SL): {stats['losses']}")
                logger.info(f"   └─ Таймауты: {stats['timeouts']}")

                if stats['wins'] and stats['losses']:
                    win_rate = stats['wins'] / (stats['wins'] + stats['losses']) * 100
                    logger.info(f"\n🏆 Win Rate: {win_rate:.1f}%")

                logger.info(f"\n💰 Фактические результаты:")
                logger.info(f"   ├─ Общий P&L: ${stats['total_pnl']:.2f}")
                logger.info(f"   ├─ Средний P&L: ${stats['avg_pnl']:.2f}")
                logger.info(f"   ├─ Средний профит: ${stats['avg_win_profit']:.2f}" if stats[
                    'avg_win_profit'] else "   ├─ Средний профит: N/A")
                logger.info(f"   └─ Средний убыток: ${stats['avg_loss']:.2f}" if stats[
                    'avg_loss'] else "   └─ Средний убыток: N/A")

                logger.info(f"\n🚀 МАКСИМАЛЬНЫЙ ПОТЕНЦИАЛ (без TP/SL):")
                logger.info(
                    f"   ├─ Средний макс. возможный профит: ${stats['avg_max_potential_profit']:.2f} ({stats['avg_max_potential_profit_pct']:.1f}%)")
                logger.info(
                    f"   ├─ Средний макс. возможный убыток: ${stats['avg_max_potential_loss']:.2f} ({stats['avg_max_potential_loss_pct']:.1f}%)")

                if stats['missed_profit_count']:
                    logger.info(f"   ├─ Сигналов с упущенным профитом: {stats['missed_profit_count']}")
                    logger.info(f"   └─ Средний упущенный профит: ${stats['avg_missed_profit']:.2f}")

                # Эффективность использования потенциала
                if stats['avg_max_potential_profit'] > 0:
                    efficiency = (stats['avg_pnl'] / stats['avg_max_potential_profit']) * 100 if stats[
                                                                                                     'avg_pnl'] > 0 else 0
                    logger.info(f"\n📊 Эффективность использования потенциала: {efficiency:.1f}%")

                if stats['avg_hours_to_close']:
                    logger.info(f"\n⏱️ Среднее время до закрытия: {stats['avg_hours_to_close']:.1f} часов")

            else:
                logger.info("Нет данных для отображения статистики")

            logger.info("=" * 70)

        except Exception as e:
            logger.error(f"❌ Ошибка при выводе статистики: {e}")

    def run(self):
        """Основной процесс анализа"""
        start_time = datetime.now()
        logger.info("🚀 Начало анализа исторических данных скоринга (v3.0)")
        logger.info(f"📅 Время запуска: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info("✨ Используется исправленный алгоритм расчета максимального потенциала")

        try:
            self.connect()

            total_unprocessed = self.get_total_unprocessed_count()

            if total_unprocessed == 0:
                logger.info("✅ Нет новых сигналов для обработки")
                return

            logger.info(f"📊 Всего необработанных сигналов: {total_unprocessed}")

            batch_size = 10000
            save_batch_size = 100
            batch_number = 0
            total_processed_in_run = 0

            while True:
                batch_number += 1
                current_unprocessed = self.get_total_unprocessed_count()

                if current_unprocessed == 0:
                    logger.info("✅ Все сигналы обработаны!")
                    break

                logger.info(f"\n📦 Обработка пакета #{batch_number}")
                logger.info(f"📊 Осталось необработанных: {current_unprocessed}")

                signals = self.get_unprocessed_signals(batch_size)

                if not signals:
                    logger.info(f"✅ Больше нет сигналов для обработки")
                    break

                logger.info(f"📊 В пакете #{batch_number}: {len(signals)} сигналов")

                results = []
                batch_processed = 0

                for i, signal in enumerate(signals):
                    if i % 100 == 0 and i > 0:
                        progress = (i / len(signals)) * 100
                        total_progress = ((total_processed_in_run + i) / total_unprocessed) * 100
                        logger.info(
                            f"⏳ Пакет #{batch_number}: {i}/{len(signals)} ({progress:.1f}%) | Общий прогресс: {total_progress:.1f}%")

                    result = self.analyze_signal(signal)
                    if result:
                        results.append(result)
                        self.processed_count += 1
                        batch_processed += 1

                    if len(results) >= save_batch_size:
                        self.save_results(results)
                        results = []

                if results:
                    self.save_results(results)

                total_processed_in_run += batch_processed
                logger.info(f"✅ Пакет #{batch_number} обработан: {batch_processed} из {len(signals)} сигналов")

                if current_unprocessed > batch_size and batch_processed > 0:
                    time.sleep(2)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info("\n" + "=" * 70)
            logger.info("📋 ИТОГИ ОБРАБОТКИ:")
            logger.info("=" * 70)
            logger.info(f"📊 Изначально к обработке: {total_unprocessed} сигналов")
            logger.info(f"✅ Успешно обработано: {self.processed_count}")
            logger.info(f"💾 Сохранено новых результатов: {self.new_signals_count}")
            logger.info(f"⭕ Пропущено (нет данных): {self.skipped_count}")
            logger.info(f"❌ Ошибок: {self.error_count}")
            logger.info(f"⏱️ Время выполнения: {duration:.1f} секунд ({duration / 60:.1f} минут)")

            if self.processed_count > 0:
                logger.info(f"⚡ Скорость обработки: {self.processed_count / duration:.1f} сигналов/сек")

            logger.info("=" * 70)

            # Выводим расширенную статистику
            self.print_enhanced_statistics()

        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            raise
        finally:
            self.disconnect()


def main():
    """Точка входа"""
    try:
        analyzer = ScoringAnalyzer(DB_CONFIG)
        analyzer.run()
    except KeyboardInterrupt:
        logger.info("\n⛔ Прерывание пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()