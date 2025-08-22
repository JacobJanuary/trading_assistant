#!/usr/bin/env python3
"""
Скрипт для анализа исторических данных скоринга
Анализирует все сигналы старше 48 часов и сохраняет результаты в БД
Version: 2.0 - Полностью переработанная логика
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
    'host': '10.8.0.1',
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
        """Получение пакета необработанных сигналов старше 48 часов
        ВАЖНО: НЕ используем OFFSET, так как обработанные записи автоматически
        исключаются через NOT EXISTS
        """
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
        """
        НОВАЯ ЛОГИКА: Определение типа сигнала ТОЛЬКО на основе total_score
        ВСЕ сигналы обрабатываются!
        """
        total_score = float(signal.get('total_score', 0))

        # КРИТИЧЕСКИ ВАЖНО: единственный критерий
        if total_score >= 0:
            return 'BUY', f"Total Score: {total_score:.1f}"
        else:
            return 'SELL', f"Total Score: {total_score:.1f}"

    def get_entry_price(self, trading_pair_id: int, signal_time: datetime,
                        signal_type: str) -> Optional[Dict]:
        """
        Получение цены входа из первой свечи ПОСЛЕ signal_time + 15 минут
        Используем fas.market_data_aggregated с timeframe='15m'
        """
        # ВАЖНО: добавляем 15 минут к времени сигнала
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
                # Для BUY используем high_price, для SELL - low_price
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

    def analyze_signal(self, signal: Dict) -> Optional[Dict]:
        """Анализ одного сигнала с использованием новой логики"""
        try:
            # ВСЕГДА определяем тип сигнала (теперь нет NO_SIGNAL)
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

            # Анализируем движение цены с использованием high/low
            result = self.process_price_history(
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

    def process_price_history(self, signal_type: str, entry_price: float,
                              history: List[Dict], actual_entry_time) -> Dict:
        """
        Обработка истории цен с использованием high/low для реалистичного расчета
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

        # Переменные для отслеживания
        is_closed = False
        close_reason = None
        close_price = None
        close_time = None
        hours_to_close = None
        is_win = None

        best_price = entry_price
        worst_price = entry_price
        max_profit_percent = 0
        max_profit_usd = 0
        max_drawdown_percent = 0
        max_drawdown_usd = 0

        # Анализируем каждую свечу
        for candle in history:
            current_time = candle['timestamp']
            hours_passed = (current_time - actual_entry_time).total_seconds() / 3600

            # ВАЖНО: используем high/low для реалистичного анализа
            high_price = float(candle['high_price'])
            low_price = float(candle['low_price'])
            close_price_candle = float(candle['close_price'])

            # Обновляем экстремумы для статистики
            if signal_type == 'BUY':
                # Для BUY: лучшая цена - максимальная, худшая - минимальная
                if high_price > best_price:
                    best_price = high_price
                    temp_profit_percent = ((best_price - entry_price) / entry_price) * 100
                    temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                    if temp_profit_usd > max_profit_usd:
                        max_profit_percent = temp_profit_percent
                        max_profit_usd = temp_profit_usd

                if low_price < worst_price:
                    worst_price = low_price
                    temp_loss_percent = ((entry_price - worst_price) / entry_price) * 100
                    temp_loss_usd = position_size * (temp_loss_percent / 100) * leverage
                    if temp_loss_usd > max_drawdown_usd:
                        max_drawdown_percent = temp_loss_percent
                        max_drawdown_usd = temp_loss_usd

                # Проверка закрытия позиции (только если еще не закрыта)
                if not is_closed:
                    # Сначала проверяем SL (приоритет защиты капитала)
                    if low_price <= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False  # ПОРАЖЕНИЕ
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    # Затем проверяем TP
                    elif high_price >= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True  # ПОБЕДА
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

            else:  # SELL
                # Для SELL: лучшая цена - минимальная, худшая - максимальная
                if low_price < best_price:
                    best_price = low_price
                    temp_profit_percent = ((entry_price - best_price) / entry_price) * 100
                    temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                    if temp_profit_usd > max_profit_usd:
                        max_profit_percent = temp_profit_percent
                        max_profit_usd = temp_profit_usd

                if high_price > worst_price:
                    worst_price = high_price
                    temp_loss_percent = ((worst_price - entry_price) / entry_price) * 100
                    temp_loss_usd = position_size * (temp_loss_percent / 100) * leverage
                    if temp_loss_usd > max_drawdown_usd:
                        max_drawdown_percent = temp_loss_percent
                        max_drawdown_usd = temp_loss_usd

                # Проверка закрытия позиции (только если еще не закрыта)
                if not is_closed:
                    # Сначала проверяем SL (приоритет защиты капитала)
                    if high_price >= sl_price:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False  # ПОРАЖЕНИЕ
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    # Затем проверяем TP
                    elif low_price <= tp_price:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True  # ПОБЕДА
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

            # Прерываем если позиция закрыта
            if is_closed:
                break

        # Если не закрылась за 48 часов
        if not is_closed:
            is_closed = True
            close_reason = 'timeout'
            is_win = None  # НЕ ОПРЕДЕЛЕНО для timeout
            close_price = float(history[-1]['close_price'])
            close_time = history[-1]['timestamp']
            hours_to_close = 48.0

        # Рассчитываем финальный P&L
        if signal_type == 'SELL':
            final_pnl_percent = ((entry_price - close_price) / entry_price) * 100
        else:  # BUY
            final_pnl_percent = ((close_price - entry_price) / entry_price) * 100

        final_pnl_usd = position_size * (final_pnl_percent / 100) * leverage

        return {
            'best_price': best_price,
            'worst_price': worst_price,
            'close_price': close_price,
            'is_closed': is_closed,
            'close_reason': close_reason,
            'is_win': is_win,
            'close_time': close_time,
            'hours_to_close': hours_to_close,
            'pnl_percent': final_pnl_percent,
            'pnl_usd': final_pnl_usd,
            'max_potential_profit_percent': max_profit_percent,
            'max_potential_profit_usd': max_profit_usd,
            'max_drawdown_percent': max_drawdown_percent,
            'max_drawdown_usd': max_drawdown_usd,
            'tp_percent': tp_percent,
            'sl_percent': sl_percent,
            'position_size': position_size,
            'leverage': leverage,
            'analysis_end_time': actual_entry_time + timedelta(hours=48)
        }

    def save_results(self, results: List[Dict]):
        """Сохранение результатов в БД с поддержкой is_win"""
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
            ON CONFLICT (scoring_history_id) DO NOTHING
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
                        result['is_win'],  # Сохраняем is_win
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

    def print_statistics(self):
        """Вывод расширенной статистики по результатам"""
        try:
            # Общая статистика
            stats_query = """
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
                    AVG(max_potential_profit_usd) as avg_max_potential,
                    AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close
                FROM web.scoring_history_results
                WHERE processed_at >= NOW() - INTERVAL '1 day'
            """

            with self.conn.cursor() as cur:
                cur.execute(stats_query)
                stats = cur.fetchone()

            logger.info("=" * 70)
            logger.info("📊 СТАТИСТИКА ЗА ПОСЛЕДНИЕ 24 ЧАСА:")
            logger.info("=" * 70)

            if stats and stats['total_signals'] > 0:
                logger.info(f"📈 Всего сигналов: {stats['total_signals']}")
                logger.info(
                    f"   ├─ BUY: {stats['buy_signals']} ({stats['buy_signals'] / stats['total_signals'] * 100:.1f}%)")
                logger.info(
                    f"   └─ SELL: {stats['sell_signals']} ({stats['sell_signals'] / stats['total_signals'] * 100:.1f}%)")

                logger.info(f"\n🎯 Результаты:")
                logger.info(f"   ├─ Победы (TP): {stats['wins']}")
                logger.info(f"   ├─ Поражения (SL): {stats['losses']}")
                logger.info(f"   └─ Таймауты: {stats['timeouts']}")

                if stats['wins'] and stats['losses']:
                    win_rate = stats['wins'] / (stats['wins'] + stats['losses']) * 100
                    logger.info(f"\n🏆 Win Rate: {win_rate:.1f}%")

                    if stats['avg_win_profit'] and stats['avg_loss']:
                        profit_factor = abs(stats['avg_win_profit'] / stats['avg_loss'])
                        logger.info(f"📊 Profit Factor: {profit_factor:.2f}")

                logger.info(f"\n💰 Финансовые показатели:")
                logger.info(f"   ├─ Общий P&L: ${stats['total_pnl']:.2f}")
                logger.info(f"   ├─ Средний P&L: ${stats['avg_pnl']:.2f}")
                logger.info(f"   ├─ Средний профит (WIN): ${stats['avg_win_profit']:.2f}" if stats[
                    'avg_win_profit'] else "   ├─ Средний профит (WIN): N/A")
                logger.info(f"   ├─ Средний убыток (LOSS): ${stats['avg_loss']:.2f}" if stats[
                    'avg_loss'] else "   ├─ Средний убыток (LOSS): N/A")
                logger.info(f"   ├─ Максимальный профит: ${stats['max_profit']:.2f}" if stats[
                    'max_profit'] else "   ├─ Максимальный профит: N/A")
                logger.info(f"   └─ Максимальный убыток: ${stats['max_loss']:.2f}" if stats[
                    'max_loss'] else "   └─ Максимальный убыток: N/A")

                if stats['avg_hours_to_close']:
                    logger.info(f"\n⏱️ Среднее время до закрытия: {stats['avg_hours_to_close']:.1f} часов")

                logger.info(f"\n📈 Средний макс. потенциал: ${stats['avg_max_potential']:.2f}" if stats[
                    'avg_max_potential'] else "\n📈 Средний макс. потенциал: N/A")
            else:
                logger.info("Нет данных для отображения статистики")

            logger.info("=" * 70)

        except Exception as e:
            logger.error(f"❌ Ошибка при выводе статистики: {e}")

    def run(self):
        """Основной процесс анализа с обработкой ВСЕХ записей"""
        start_time = datetime.now()
        logger.info("🚀 Начало анализа исторических данных скоринга")
        logger.info(f"📅 Время запуска: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")

        try:
            self.connect()

            # Получаем общее количество необработанных сигналов
            total_unprocessed = self.get_total_unprocessed_count()

            if total_unprocessed == 0:
                logger.info("✅ Нет новых сигналов для обработки")
                return

            logger.info(f"📊 Всего необработанных сигналов: {total_unprocessed}")
            logger.info(f"📦 Будут обработаны пакетами по 10000 записей")

            # Параметры пакетной обработки
            batch_size = 10000
            save_batch_size = 100
            batch_number = 0
            total_processed_in_run = 0
            previous_unprocessed = total_unprocessed
            no_progress_counter = 0

            # Обрабатываем все записи пакетами
            # ВАЖНО: НЕ используем offset, так как обработанные записи
            # автоматически исключаются из выборки
            while True:
                batch_number += 1

                # Получаем текущее количество необработанных
                current_unprocessed = self.get_total_unprocessed_count()

                if current_unprocessed == 0:
                    logger.info("✅ Все сигналы обработаны!")
                    break

                logger.info(f"\n📦 Обработка пакета #{batch_number}")
                logger.info(f"📊 Осталось необработанных: {current_unprocessed}")

                # Получаем следующий пакет (всегда без offset!)
                signals = self.get_unprocessed_signals(batch_size)

                if not signals:
                    logger.info(f"✅ Больше нет сигналов для обработки")
                    break

                logger.info(f"📊 В пакете #{batch_number}: {len(signals)} сигналов")

                # Анализируем сигналы в пакете
                results = []
                batch_processed = 0

                for i, signal in enumerate(signals):
                    # Прогресс внутри пакета
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

                    # Сохраняем результаты пачками
                    if len(results) >= save_batch_size:
                        self.save_results(results)
                        results = []

                # Сохраняем оставшиеся результаты пакета
                if results:
                    self.save_results(results)

                total_processed_in_run += batch_processed
                logger.info(f"✅ Пакет #{batch_number} обработан: {batch_processed} из {len(signals)} сигналов")

                # Если обработали меньше чем получили, возможно проблемы с данными
                if batch_processed < len(signals):
                    skipped_in_batch = len(signals) - batch_processed
                    logger.warning(f"⚠️ В пакете #{batch_number} пропущено {skipped_in_batch} сигналов (нет данных)")

                # ЗАЩИТА ОТ ЗАЦИКЛИВАНИЯ
                # Если количество необработанных не изменилось после пакета
                if current_unprocessed == previous_unprocessed:
                    no_progress_counter += 1
                    if no_progress_counter >= 3:
                        logger.warning(f"⚠️ Обработка остановлена: {current_unprocessed} записей невозможно обработать")
                        logger.warning(f"   Вероятно, для них отсутствуют данные в market_data_aggregated")

                        # Показываем проблемные пары для диагностики
                        with self.conn.cursor() as cur:
                            cur.execute("""
                                SELECT DISTINCT pair_symbol, COUNT(*) as count
                                FROM fas.scoring_history sh
                                WHERE sh.timestamp <= NOW() - INTERVAL '48 hours'
                                    AND NOT EXISTS (
                                        SELECT 1 FROM web.scoring_history_results shr
                                        WHERE shr.scoring_history_id = sh.id
                                    )
                                GROUP BY pair_symbol
                                ORDER BY count DESC
                                LIMIT 10
                            """)
                            problem_pairs = cur.fetchall()

                            logger.warning("   Проблемные пары:")
                            for pair in problem_pairs:
                                logger.warning(f"     - {pair['pair_symbol']}: {pair['count']} записей")
                        break
                else:
                    no_progress_counter = 0
                    previous_unprocessed = current_unprocessed

                # Небольшая пауза между пакетами для снижения нагрузки (только если есть прогресс)
                if current_unprocessed > batch_size and batch_processed > 0:
                    logger.info(f"⏸️ Пауза перед следующим пакетом...")
                    time.sleep(2)

            # Выводим итоговую статистику
            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info("\n" + "=" * 70)
            logger.info("📋 ИТОГИ ОБРАБОТКИ:")
            logger.info("=" * 70)
            logger.info(f"📊 Изначально к обработке: {total_unprocessed} сигналов")
            logger.info(f"✅ Успешно обработано: {self.processed_count}")
            logger.info(f"💾 Сохранено новых результатов: {self.new_signals_count}")
            logger.info(f"⏭️ Пропущено (нет данных): {self.skipped_count}")
            logger.info(f"❌ Ошибок: {self.error_count}")
            logger.info(f"📦 Обработано пакетов: {batch_number}")
            logger.info(f"⏱️ Время выполнения: {duration:.1f} секунд ({duration / 60:.1f} минут)")

            if self.processed_count > 0:
                logger.info(f"⚡ Скорость обработки: {self.processed_count / duration:.1f} сигналов/сек")

            # Финальная проверка
            final_unprocessed = self.get_total_unprocessed_count()
            if final_unprocessed > 0:
                logger.warning(f"\n⚠️ Остались необработанные сигналы: {final_unprocessed}")
                logger.warning(f"   Это записи, для которых отсутствуют данные в market_data_aggregated")

                # Показываем детали по необработанным
                with self.conn.cursor() as cur:
                    cur.execute("""
                        SELECT 
                            pair_symbol,
                            COUNT(*) as count,
                            MIN(timestamp) as min_date,
                            MAX(timestamp) as max_date
                        FROM fas.scoring_history sh
                        WHERE sh.timestamp <= NOW() - INTERVAL '48 hours'
                            AND NOT EXISTS (
                                SELECT 1 FROM web.scoring_history_results shr
                                WHERE shr.scoring_history_id = sh.id
                            )
                        GROUP BY pair_symbol
                        ORDER BY count DESC
                    """)
                    unprocessed_pairs = cur.fetchall()

                    logger.warning("\n   Детализация по парам:")
                    for pair in unprocessed_pairs[:20]:  # Показываем топ-20
                        logger.warning(
                            f"     {pair['pair_symbol']:20s}: {pair['count']:5d} записей ({pair['min_date'].strftime('%Y-%m-%d')} - {pair['max_date'].strftime('%Y-%m-%d')})")

            logger.info("=" * 70)

            # Выводим статистику по результатам
            self.print_statistics()

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