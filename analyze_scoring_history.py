#!/usr/bin/env python3
"""
Скрипт для анализа исторических данных скоринга
Анализирует все сигналы старше 48 часов и сохраняет результаты в БД
"""

import os
import sys
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
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
    'analysis_hours': 48  # Анализируем 48 часов после сигнала
}

# Критерии определения сигналов
SIGNAL_CRITERIA = {
    'BUY': [
        {'market': 'BULL', 'total_score_min': 30},
        {'market': 'BULL', 'indicator_score_min': 40},
        {'market': 'NEUTRAL', 'total_score_min': 50},
    ],
    'SELL': [
        {'market': 'BEAR', 'total_score_max': -30},
        {'market': 'BEAR', 'indicator_score_max': -40},
        {'market': 'NEUTRAL', 'total_score_max': -50},
    ]
}


class ScoringAnalyzer:
    def __init__(self, db_config: dict):
        self.db_config = db_config
        self.conn = None
        self.processed_count = 0
        self.error_count = 0
        self.new_signals_count = 0

    def connect(self):
        """Подключение к БД"""
        try:
            conn_string = f"host={self.db_config['host']} port={self.db_config['port']} " \
                          f"dbname={self.db_config['dbname']} user={self.db_config['user']} " \
                          f"password={self.db_config['password']}"
            self.conn = psycopg.connect(conn_string, row_factory=dict_row)
            logger.info("Успешное подключение к БД")
        except Exception as e:
            logger.error(f"Ошибка подключения к БД: {e}")
            raise

    def disconnect(self):
        """Отключение от БД"""
        if self.conn:
            self.conn.close()
            logger.info("Отключение от БД")

    def get_unprocessed_signals(self) -> List[Dict]:
        """Получение необработанных сигналов старше 48 часов"""
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
            LIMIT 10000  -- Обрабатываем порциями
        """

        with self.conn.cursor() as cur:
            cur.execute(query)
            signals = cur.fetchall()

        logger.info(f"Найдено {len(signals)} необработанных сигналов")
        return signals

    def determine_signal_type(self, signal: Dict) -> Tuple[str, str]:
        """Определение типа сигнала на основе критериев"""
        market_regime = signal.get('market_regime')
        total_score = float(signal.get('total_score', 0))
        indicator_score = float(signal.get('indicator_score', 0))
        pattern_score = float(signal.get('pattern_score', 0))

        # Проверяем критерии для BUY
        for criteria in SIGNAL_CRITERIA['BUY']:
            if 'market' in criteria and market_regime != criteria['market']:
                continue
            if 'total_score_min' in criteria and total_score < criteria['total_score_min']:
                continue
            if 'indicator_score_min' in criteria and indicator_score < criteria['indicator_score_min']:
                continue
            # Если все условия выполнены
            return 'BUY', f"Market: {market_regime}, Total: {total_score:.1f}"

        # Проверяем критерии для SELL
        for criteria in SIGNAL_CRITERIA['SELL']:
            if 'market' in criteria and market_regime != criteria['market']:
                continue
            if 'total_score_max' in criteria and total_score > criteria['total_score_max']:
                continue
            if 'indicator_score_max' in criteria and indicator_score > criteria['indicator_score_max']:
                continue
            # Если все условия выполнены
            return 'SELL', f"Market: {market_regime}, Total: {total_score:.1f}"

        return 'NO_SIGNAL', 'No criteria matched'

    def analyze_signal(self, signal: Dict) -> Optional[Dict]:
        """Анализ одного сигнала"""
        try:
            # Определяем тип сигнала
            signal_type, signal_criteria = self.determine_signal_type(signal)

            # Если нет сигнала, пропускаем
            if signal_type == 'NO_SIGNAL':
                return None

            # Получаем цену входа
            entry_price_query = """
                SELECT mark_price
                FROM public.market_data
                WHERE trading_pair_id = %s
                    AND capture_time >= %s - INTERVAL '5 minutes'
                    AND capture_time <= %s + INTERVAL '5 minutes'
                ORDER BY ABS(EXTRACT(EPOCH FROM (capture_time - %s))) ASC
                LIMIT 1
            """

            with self.conn.cursor() as cur:
                cur.execute(entry_price_query, (
                    signal['trading_pair_id'],
                    signal['signal_timestamp'],
                    signal['signal_timestamp'],
                    signal['signal_timestamp']
                ))
                price_result = cur.fetchone()

            if not price_result:
                logger.warning(f"Нет цены входа для {signal['pair_symbol']} @ {signal['signal_timestamp']}")
                return None

            entry_price = float(price_result['mark_price'])

            # Получаем историю цен за 48 часов
            history_query = """
                SELECT capture_time, mark_price
                FROM public.market_data
                WHERE trading_pair_id = %s
                    AND capture_time >= %s
                    AND capture_time <= %s + INTERVAL '48 hours'
                ORDER BY capture_time ASC
            """

            with self.conn.cursor() as cur:
                cur.execute(history_query, (
                    signal['trading_pair_id'],
                    signal['signal_timestamp'],
                    signal['signal_timestamp']
                ))
                history = cur.fetchall()

            if not history:
                logger.warning(f"Нет истории цен для {signal['pair_symbol']}")
                return None

            # Анализируем движение цены
            result = self.process_price_history(
                signal_type,
                entry_price,
                history,
                signal['signal_timestamp']
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
            logger.error(f"Ошибка анализа сигнала {signal['pair_symbol']}: {e}")
            return None

    def process_price_history(self, signal_type: str, entry_price: float,
                              history: List[Dict], signal_timestamp) -> Dict:
        """Обработка истории цен для сигнала"""
        tp_percent = ANALYSIS_PARAMS['tp_percent']
        sl_percent = ANALYSIS_PARAMS['sl_percent']
        position_size = ANALYSIS_PARAMS['position_size']
        leverage = ANALYSIS_PARAMS['leverage']

        # Переменные для отслеживания
        is_closed = False
        close_reason = None
        close_price = None
        close_time = None
        hours_to_close = None
        is_win = None  # НОВАЯ ПЕРЕМЕННАЯ

        best_price = entry_price
        worst_price = entry_price
        max_profit_percent = 0
        max_profit_usd = 0
        max_drawdown_percent = 0
        max_drawdown_usd = 0

        # Анализируем каждую точку истории
        for price_point in history:
            current_price = float(price_point['mark_price'])
            current_time = price_point['capture_time']
            hours_passed = (current_time - signal_timestamp).total_seconds() / 3600

            # Обновляем лучшую и худшую цены
            if signal_type == 'SELL':
                if current_price < best_price:
                    best_price = current_price
                    temp_profit_percent = ((entry_price - best_price) / entry_price) * 100
                    temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                    if temp_profit_usd > max_profit_usd:
                        max_profit_percent = temp_profit_percent
                        max_profit_usd = temp_profit_usd

                if current_price > worst_price:
                    worst_price = current_price
                    temp_loss_percent = ((worst_price - entry_price) / entry_price) * 100
                    temp_loss_usd = position_size * (temp_loss_percent / 100) * leverage
                    if temp_loss_usd > max_drawdown_usd:
                        max_drawdown_percent = temp_loss_percent
                        max_drawdown_usd = temp_loss_usd
            else:  # BUY
                if current_price > best_price:
                    best_price = current_price
                    temp_profit_percent = ((best_price - entry_price) / entry_price) * 100
                    temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                    if temp_profit_usd > max_profit_usd:
                        max_profit_percent = temp_profit_percent
                        max_profit_usd = temp_profit_usd

                if current_price < worst_price:
                    worst_price = current_price
                    temp_loss_percent = ((entry_price - worst_price) / entry_price) * 100
                    temp_loss_usd = position_size * (temp_loss_percent / 100) * leverage
                    if temp_loss_usd > max_drawdown_usd:
                        max_drawdown_percent = temp_loss_percent
                        max_drawdown_usd = temp_loss_usd

            # Проверяем условия закрытия (только если еще не закрыта)
            if not is_closed:
                if signal_type == 'SELL':
                    price_change_percent = ((entry_price - current_price) / entry_price) * 100
                else:  # BUY
                    price_change_percent = ((current_price - entry_price) / entry_price) * 100

                # Проверяем TP
                if price_change_percent >= tp_percent:
                    is_closed = True
                    close_reason = 'take_profit'
                    is_win = True  # ПОБЕДА!
                    close_price = current_price
                    close_time = current_time
                    hours_to_close = hours_passed
                # Проверяем SL
                elif price_change_percent <= -sl_percent:
                    is_closed = True
                    close_reason = 'stop_loss'
                    is_win = False  # ПОРАЖЕНИЕ!
                    close_price = current_price
                    close_time = current_time
                    hours_to_close = hours_passed

        # Если не закрылась за 48 часов
        if not is_closed:
            is_closed = True
            close_reason = 'timeout'
            is_win = None  # НЕ ОПРЕДЕЛЕНО (timeout)
            close_price = float(history[-1]['mark_price'])
            close_time = history[-1]['capture_time']
            hours_to_close = 48.0

        # Рассчитываем финальный P&L
        if signal_type == 'SELL':
            final_pnl_percent = ((entry_price - close_price) / entry_price) * 100
        else:
            final_pnl_percent = ((close_price - entry_price) / entry_price) * 100

        final_pnl_usd = position_size * (final_pnl_percent / 100) * leverage

        return {
            'best_price': best_price,
            'worst_price': worst_price,
            'close_price': close_price,
            'is_closed': is_closed,
            'close_reason': close_reason,
            'is_win': is_win,  # ДОБАВЛЯЕМ В РЕЗУЛЬТАТ
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
            'analysis_end_time': signal_timestamp + timedelta(hours=48)
        }

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
            ON CONFLICT (scoring_history_id) DO NOTHING
        """

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
                        result['is_win'],  # ДОБАВЛЯЕМ is_win
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
                    self.new_signals_count += 1
                except Exception as e:
                    logger.error(f"Ошибка сохранения результата: {e}")
                    self.error_count += 1

        self.conn.commit()
        logger.info(f"Сохранено {self.new_signals_count} новых результатов")

    def print_statistics(self):
        """Вывод статистики по результатам"""
        stats_query = """
            SELECT 
                COUNT(*) as total_signals,
                COUNT(CASE WHEN signal_type = 'BUY' THEN 1 END) as buy_signals,
                COUNT(CASE WHEN signal_type = 'SELL' THEN 1 END) as sell_signals,
                COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as tp_count,
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
                COUNT(CASE WHEN close_reason = 'timeout' THEN 1 END) as timeout_count,
                AVG(pnl_usd) as avg_pnl,
                SUM(pnl_usd) as total_pnl,
                AVG(CASE WHEN close_reason = 'take_profit' THEN pnl_usd END) as avg_tp_profit,
                AVG(CASE WHEN close_reason = 'stop_loss' THEN pnl_usd END) as avg_sl_loss,
                AVG(max_potential_profit_usd) as avg_max_profit,
                AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close
            FROM web.scoring_history_results
            WHERE processed_at >= NOW() - INTERVAL '1 day'
        """

        with self.conn.cursor() as cur:
            cur.execute(stats_query)
            stats = cur.fetchone()

        logger.info("=" * 60)
        logger.info("СТАТИСТИКА ЗА ПОСЛЕДНИЕ 24 ЧАСА:")
        logger.info(f"Всего сигналов: {stats['total_signals']}")
        logger.info(f"BUY: {stats['buy_signals']}, SELL: {stats['sell_signals']}")
        logger.info(f"TP: {stats['tp_count']}, SL: {stats['sl_count']}, Timeout: {stats['timeout_count']}")

        if stats['tp_count'] + stats['sl_count'] > 0:
            win_rate = stats['tp_count'] / (stats['tp_count'] + stats['sl_count']) * 100
            logger.info(f"Win Rate: {win_rate:.1f}%")

        logger.info(f"Средний P&L: ${stats['avg_pnl']:.2f}")
        logger.info(f"Общий P&L: ${stats['total_pnl']:.2f}")
        logger.info(f"Средний профит на TP: ${stats['avg_tp_profit']:.2f}")
        logger.info(f"Средний убыток на SL: ${stats['avg_sl_loss']:.2f}")
        logger.info(f"Средний макс. профит: ${stats['avg_max_profit']:.2f}")
        logger.info(f"Среднее время до закрытия: {stats['avg_hours_to_close']:.1f} часов")
        logger.info("=" * 60)

    def run(self):
        """Основной процесс анализа"""
        logger.info("Начало анализа исторических данных скоринга")

        try:
            self.connect()

            # Получаем необработанные сигналы
            signals = self.get_unprocessed_signals()

            if not signals:
                logger.info("Нет новых сигналов для обработки")
                return

            # Анализируем сигналы
            results = []
            for i, signal in enumerate(signals):
                if i % 100 == 0:
                    logger.info(f"Обработано {i}/{len(signals)} сигналов...")

                result = self.analyze_signal(signal)
                if result:
                    results.append(result)
                    self.processed_count += 1

                # Сохраняем пачками по 100
                if len(results) >= 100:
                    self.save_results(results)
                    results = []

            # Сохраняем оставшиеся результаты
            if results:
                self.save_results(results)

            # Выводим статистику
            logger.info(f"\nОбработано сигналов: {self.processed_count}")
            logger.info(f"Сохранено новых результатов: {self.new_signals_count}")
            logger.info(f"Ошибок: {self.error_count}")

            self.print_statistics()

        except Exception as e:
            logger.error(f"Критическая ошибка: {e}")
            raise
        finally:
            self.disconnect()


def main():
    """Точка входа"""
    analyzer = ScoringAnalyzer(DB_CONFIG)
    analyzer.run()


if __name__ == "__main__":
    main()