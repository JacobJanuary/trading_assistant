#!/usr/bin/env python3
"""
Улучшенный скрипт для анализа исторических данных скоринга
Рассчитывает результаты одновременно для LONG и SHORT позиций
Version: 4.0
"""

import os
import sys
import psycopg
from psycopg.rows import dict_row
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import logging
import time
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, asdict
import json
from pathlib import Path

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('analyze_scoring_history.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


@dataclass
class AnalysisConfig:
    """Конфигурация параметров анализа"""
    tp_percent: float = 3.0
    sl_percent: float = 3.0
    position_size: float = 100.0
    leverage: int = 5
    analysis_hours: int = 48
    entry_delay_minutes: int = 15
    batch_size: int = 10000
    save_batch_size: int = 100


@dataclass
class TradeResult:
    """Результат торговли для одного направления"""
    direction: str  # 'LONG' или 'SHORT'
    entry_price: float
    best_price: float
    worst_price: float
    close_price: float
    is_closed: bool
    close_reason: str
    is_win: Optional[bool]
    close_time: Optional[datetime]
    hours_to_close: Optional[float]
    pnl_percent: float
    pnl_usd: float
    max_potential_profit_percent: float
    max_potential_profit_usd: float
    max_drawdown_percent: float
    max_drawdown_usd: float
    absolute_max_price: float
    absolute_min_price: float
    time_to_max_hours: float
    time_to_min_hours: float


class ImprovedScoringAnalyzer:
    def __init__(self, config_path: str = "config.json"):
        """
        Инициализация с загрузкой конфигурации из файла
        """
        self.config = AnalysisConfig()
        self.db_config = self._load_db_config(config_path)
        self.conn = None
        self.processed_count = 0
        self.error_count = 0
        self.new_signals_count = 0
        self.skipped_count = 0

    def _load_db_config(self, config_path: str) -> dict:
        """Загрузка конфигурации БД из файла"""
        config_file = Path(config_path)

        # Если файл не существует, используем переменные окружения
        if not config_file.exists():
            return {
                'host': os.getenv('DB_HOST', 'localhost'),
                'port': int(os.getenv('DB_PORT', 5432)),
                'dbname': os.getenv('DB_NAME', 'fox_crypto'),
                'user': os.getenv('DB_USER', 'elcrypto'),
                'password': os.getenv('DB_PASSWORD', 'LohNeMamont@!21')
            }

        with open(config_file, 'r') as f:
            config = json.load(f)
        return config.get('database', {})

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
                    SELECT 1 FROM web.scoring_history_results_v2 shr
                    WHERE shr.scoring_history_id = sh.id
                )
            ORDER BY sh.timestamp ASC
            LIMIT %s
        """

        with self.conn.cursor() as cur:
            cur.execute(query, (batch_size,))
            signals = cur.fetchall()

        return signals

    def get_entry_price(self, trading_pair_id: int, signal_time: datetime,
                        direction: str) -> Optional[Dict]:
        """
        Получение цены входа для указанного направления
        Используем среднее значение между high_price и low_price для обоих направлений
        """
        entry_time = signal_time + timedelta(minutes=self.config.entry_delay_minutes)

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
                # Используем среднее значение между high и low для обоих направлений
                high_price = float(result['high_price'])
                low_price = float(result['low_price'])
                entry_price = (high_price + low_price) / 2

                return {
                    'entry_price': entry_price,
                    'entry_time': result['timestamp']
                }
            return None

        except Exception as e:
            logger.error(f"❌ Ошибка получения цены входа: {e}")
            return None

    def calculate_trade_result(self, direction: str, entry_price: float,
                               history: List[Dict], actual_entry_time: datetime) -> TradeResult:
        """
        Универсальный расчет результата торговли для указанного направления
        """
        tp_percent = self.config.tp_percent
        sl_percent = self.config.sl_percent
        position_size = self.config.position_size
        leverage = self.config.leverage

        # Расчет уровней TP и SL
        if direction == 'LONG':
            tp_price = entry_price * (1 + tp_percent / 100)
            sl_price = entry_price * (1 - sl_percent / 100)
        else:  # SHORT
            tp_price = entry_price * (1 - tp_percent / 100)
            sl_price = entry_price * (1 + sl_percent / 100)

        # Инициализация переменных
        is_closed = False
        close_reason = None
        close_price = None
        close_time = None
        hours_to_close = None
        is_win = None

        # Переменные для отслеживания экстремумов
        absolute_max_price = entry_price
        absolute_min_price = entry_price
        time_to_max = 0
        time_to_min = 0

        # Переменные для трекинга максимальной просадки
        running_best_price = entry_price
        max_drawdown_from_peak = 0

        # Анализ истории цен
        for i, candle in enumerate(history):
            current_time = candle['timestamp']
            hours_passed = (current_time - actual_entry_time).total_seconds() / 3600

            high_price = float(candle['high_price'])
            low_price = float(candle['low_price'])

            # Обновляем абсолютные экстремумы
            if high_price > absolute_max_price:
                absolute_max_price = high_price
                time_to_max = hours_passed

            if low_price < absolute_min_price:
                absolute_min_price = low_price
                time_to_min = hours_passed

            # Проверяем закрытие позиции (только если еще не закрыта)
            if not is_closed:
                if direction == 'LONG':
                    # Проверяем, достигнуты ли оба уровня в одной свече
                    sl_hit = low_price <= sl_price
                    tp_hit = high_price >= tp_price
                    
                    if sl_hit and tp_hit:
                        # Оба уровня достигнуты - используем консервативный подход (SL)
                        # В реальности нужно смотреть на более мелкий таймфрейм
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    elif sl_hit:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    elif tp_hit:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

                else:  # SHORT
                    # Проверяем, достигнуты ли оба уровня в одной свече
                    sl_hit = high_price >= sl_price
                    tp_hit = low_price <= tp_price
                    
                    if sl_hit and tp_hit:
                        # Оба уровня достигнуты - используем консервативный подход (SL)
                        # В реальности нужно смотреть на более мелкий таймфрейм
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    elif sl_hit:
                        is_closed = True
                        close_reason = 'stop_loss'
                        is_win = False
                        close_price = sl_price
                        close_time = current_time
                        hours_to_close = hours_passed
                    elif tp_hit:
                        is_closed = True
                        close_reason = 'take_profit'
                        is_win = True
                        close_price = tp_price
                        close_time = current_time
                        hours_to_close = hours_passed

            # Обновляем running best и считаем просадку
            if direction == 'LONG':
                if high_price > running_best_price:
                    running_best_price = high_price
                current_drawdown = ((running_best_price - low_price) / running_best_price) * 100
                if current_drawdown > max_drawdown_from_peak:
                    max_drawdown_from_peak = current_drawdown
            else:  # SHORT
                if low_price < running_best_price:
                    running_best_price = low_price
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

        # Расчет финансовых показателей
        if direction == 'LONG':
            # Максимальный потенциальный профит и убыток для LONG
            max_potential_profit_percent = ((absolute_max_price - entry_price) / entry_price) * 100
            max_potential_profit_usd = position_size * leverage * (max_potential_profit_percent / 100)
            max_potential_loss_percent = ((entry_price - absolute_min_price) / entry_price) * 100
            max_potential_loss_usd = position_size * leverage * (max_potential_loss_percent / 100)

            # Фактический P&L
            final_pnl_percent = ((close_price - entry_price) / entry_price) * 100
            best_price = absolute_max_price
            worst_price = absolute_min_price

        else:  # SHORT
            # Максимальный потенциальный профит и убыток для SHORT
            max_potential_profit_percent = ((entry_price - absolute_min_price) / entry_price) * 100
            max_potential_profit_usd = position_size * leverage * (max_potential_profit_percent / 100)
            max_potential_loss_percent = ((absolute_max_price - entry_price) / entry_price) * 100
            max_potential_loss_usd = position_size * leverage * (max_potential_loss_percent / 100)

            # Фактический P&L
            final_pnl_percent = ((entry_price - close_price) / entry_price) * 100
            best_price = absolute_min_price
            worst_price = absolute_max_price

        final_pnl_usd = position_size * leverage * (final_pnl_percent / 100)

        return TradeResult(
            direction=direction,
            entry_price=entry_price,
            best_price=best_price,
            worst_price=worst_price,
            close_price=close_price,
            is_closed=is_closed,
            close_reason=close_reason,
            is_win=is_win,
            close_time=close_time,
            hours_to_close=hours_to_close,
            pnl_percent=final_pnl_percent,
            pnl_usd=final_pnl_usd,
            max_potential_profit_percent=max_potential_profit_percent,
            max_potential_profit_usd=max_potential_profit_usd,
            max_drawdown_percent=max_potential_loss_percent,
            max_drawdown_usd=max_potential_loss_usd,
            absolute_max_price=absolute_max_price,
            absolute_min_price=absolute_min_price,
            time_to_max_hours=time_to_max,
            time_to_min_hours=time_to_min
        )

    def create_no_data_result(self, signal: Dict, direction: str, reason: str) -> Dict:
        """
        Создает запись для сигналов без данных, чтобы они не обрабатывались повторно
        """
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
            'signal_type': direction,
            'entry_price': None,
            'best_price': None,
            'worst_price': None,
            'close_price': None,
            'is_closed': False,
            'close_reason': reason,  # 'no_entry_price' или 'insufficient_history'
            'is_win': None,
            'close_time': None,
            'hours_to_close': None,
            'pnl_percent': 0,
            'pnl_usd': 0,
            'max_potential_profit_percent': 0,
            'max_potential_profit_usd': 0,
            'max_drawdown_percent': 0,
            'max_drawdown_usd': 0,
            'tp_percent': self.config.tp_percent,
            'sl_percent': self.config.sl_percent,
            'position_size': self.config.position_size,
            'leverage': self.config.leverage,
            'analysis_end_time': signal['signal_timestamp'] + timedelta(hours=48)
        }

    def analyze_signal_both_directions(self, signal: Dict) -> Tuple[Optional[Dict], Optional[Dict]]:
        """
        Анализ сигнала для обоих направлений (LONG и SHORT)
        Возвращает два результата - для LONG и SHORT позиций
        """
        try:
            # Получаем цену входа (одинаковая для обоих направлений)
            entry_data = self.get_entry_price(
                signal['trading_pair_id'],
                signal['signal_timestamp'],
                'LONG'  # Направление больше не влияет на цену
            )

            # Если нет данных о цене входа - создаем записи с пометкой NO_DATA
            if not entry_data:
                logger.warning(f"⚠️ Нет цены входа для {signal['pair_symbol']} @ {signal['signal_timestamp']}")
                self.skipped_count += 1
                # ВАЖНО: Возвращаем записи с пометкой no_entry_price, чтобы не зацикливаться
                long_result = self.create_no_data_result(signal, 'LONG', 'no_entry_price')
                short_result = self.create_no_data_result(signal, 'SHORT', 'no_entry_price')
                return long_result, short_result

            # Берем время и цену входа (одинаковые для обоих направлений)
            actual_entry_time = entry_data['entry_time']
            entry_price = entry_data['entry_price']

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
                    signal['trading_pair_id'],
                    actual_entry_time,
                    actual_entry_time
                ))
                history = cur.fetchall()

            if not history or len(history) < 10:
                logger.warning(f"⚠️ Недостаточно истории для {signal['pair_symbol']}")
                self.skipped_count += 1
                # ВАЖНО: Возвращаем записи с пометкой insufficient_history, чтобы не зацикливаться
                long_result = self.create_no_data_result(signal, 'LONG', 'insufficient_history')
                short_result = self.create_no_data_result(signal, 'SHORT', 'insufficient_history')
                return long_result, short_result

            # Рассчитываем результаты для обоих направлений
            long_result = self.calculate_trade_result(
                'LONG',
                entry_price,
                history,
                actual_entry_time
            )

            short_result = self.calculate_trade_result(
                'SHORT',
                entry_price,
                history,
                actual_entry_time
            )

            # Формируем результаты для сохранения
            base_data = {
                'scoring_history_id': signal['scoring_history_id'],
                'signal_timestamp': signal['signal_timestamp'],
                'pair_symbol': signal['pair_symbol'],
                'trading_pair_id': signal['trading_pair_id'],
                'market_regime': signal['market_regime'],
                'total_score': float(signal['total_score']),
                'indicator_score': float(signal['indicator_score']),
                'pattern_score': float(signal['pattern_score']),
                'combination_score': float(signal.get('combination_score', 0)),
                'tp_percent': self.config.tp_percent,
                'sl_percent': self.config.sl_percent,
                'position_size': self.config.position_size,
                'leverage': self.config.leverage,
                'analysis_end_time': actual_entry_time + timedelta(hours=48)
            }

            # Результат для LONG
            long_data = {**base_data}
            long_data.update({
                'signal_type': 'LONG',
                'entry_price': long_result.entry_price,
                'best_price': long_result.best_price,
                'worst_price': long_result.worst_price,
                'close_price': long_result.close_price,
                'is_closed': long_result.is_closed,
                'close_reason': long_result.close_reason,
                'is_win': long_result.is_win,
                'close_time': long_result.close_time,
                'hours_to_close': long_result.hours_to_close,
                'pnl_percent': long_result.pnl_percent,
                'pnl_usd': long_result.pnl_usd,
                'max_potential_profit_percent': long_result.max_potential_profit_percent,
                'max_potential_profit_usd': long_result.max_potential_profit_usd,
                'max_drawdown_percent': long_result.max_drawdown_percent,
                'max_drawdown_usd': long_result.max_drawdown_usd
            })

            # Результат для SHORT
            short_data = {**base_data}
            short_data.update({
                'signal_type': 'SHORT',
                'entry_price': short_result.entry_price,
                'best_price': short_result.best_price,
                'worst_price': short_result.worst_price,
                'close_price': short_result.close_price,
                'is_closed': short_result.is_closed,
                'close_reason': short_result.close_reason,
                'is_win': short_result.is_win,
                'close_time': short_result.close_time,
                'hours_to_close': short_result.hours_to_close,
                'pnl_percent': short_result.pnl_percent,
                'pnl_usd': short_result.pnl_usd,
                'max_potential_profit_percent': short_result.max_potential_profit_percent,
                'max_potential_profit_usd': short_result.max_potential_profit_usd,
                'max_drawdown_percent': short_result.max_drawdown_percent,
                'max_drawdown_usd': short_result.max_drawdown_usd
            })

            return long_data, short_data

        except Exception as e:
            logger.error(f"❌ Ошибка анализа сигнала {signal['pair_symbol']}: {e}")
            self.error_count += 1
            return None, None

    def save_results(self, results: List[Dict]):
        """
        Сохранение результатов в новую таблицу с поддержкой обоих направлений
        """
        if not results:
            return

        # Создаем новую таблицу если ее нет
        create_table_query = """
            CREATE TABLE IF NOT EXISTS web.scoring_history_results_v2 (
                id SERIAL PRIMARY KEY,
                scoring_history_id INTEGER NOT NULL,
                signal_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
                pair_symbol VARCHAR(50) NOT NULL,
                trading_pair_id INTEGER NOT NULL,
                market_regime VARCHAR(50),
                total_score DECIMAL(10,2),
                indicator_score DECIMAL(10,2),
                pattern_score DECIMAL(10,2),
                combination_score DECIMAL(10,2),
                signal_type VARCHAR(10) NOT NULL, -- 'LONG' или 'SHORT'
                entry_price DECIMAL(20,8),
                best_price DECIMAL(20,8),
                worst_price DECIMAL(20,8),
                close_price DECIMAL(20,8),
                is_closed BOOLEAN DEFAULT FALSE,
                close_reason VARCHAR(50),
                is_win BOOLEAN,
                close_time TIMESTAMP WITH TIME ZONE,
                hours_to_close DECIMAL(10,2),
                pnl_percent DECIMAL(10,4),
                pnl_usd DECIMAL(15,2),
                max_potential_profit_percent DECIMAL(10,4),
                max_potential_profit_usd DECIMAL(15,2),
                max_drawdown_percent DECIMAL(10,4),
                max_drawdown_usd DECIMAL(15,2),
                tp_percent DECIMAL(5,2),
                sl_percent DECIMAL(5,2),
                position_size DECIMAL(10,2),
                leverage INTEGER,
                analysis_end_time TIMESTAMP WITH TIME ZONE,
                processed_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                UNIQUE(scoring_history_id, signal_type)
            );

            CREATE INDEX IF NOT EXISTS idx_scoring_results_v2_timestamp 
                ON web.scoring_history_results_v2(signal_timestamp);
            CREATE INDEX IF NOT EXISTS idx_scoring_results_v2_pair 
                ON web.scoring_history_results_v2(trading_pair_id);
            CREATE INDEX IF NOT EXISTS idx_scoring_results_v2_signal_type 
                ON web.scoring_history_results_v2(signal_type);
        """

        with self.conn.cursor() as cur:
            cur.execute(create_table_query)

        insert_query = """
            INSERT INTO web.scoring_history_results_v2 (
                scoring_history_id, signal_timestamp, pair_symbol, trading_pair_id,
                market_regime, total_score, indicator_score, pattern_score, combination_score,
                signal_type, entry_price, best_price, worst_price, close_price,
                is_closed, close_reason, is_win, close_time, hours_to_close,
                pnl_percent, pnl_usd,
                max_potential_profit_percent, max_potential_profit_usd,
                max_drawdown_percent, max_drawdown_usd,
                tp_percent, sl_percent, position_size, leverage,
                analysis_end_time
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (scoring_history_id, signal_type) DO UPDATE SET
                pnl_percent = EXCLUDED.pnl_percent,
                pnl_usd = EXCLUDED.pnl_usd,
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
                        result['entry_price'],
                        result['best_price'],
                        result['worst_price'],
                        result['close_price'],
                        result['is_closed'],
                        result['close_reason'],
                        result.get('is_win'),
                        result.get('close_time'),
                        result.get('hours_to_close'),
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
                    logger.error(f"❌ Ошибка сохранения результата: {e}")
                    self.error_count += 1

        self.conn.commit()
        self.new_signals_count += saved_count
        logger.info(f"💾 Сохранено {saved_count} результатов из {len(results)}")

    def print_comparative_statistics(self):
        """Вывод сравнительной статистики по LONG и SHORT позициям"""
        try:
            stats_query = """
                WITH stats AS (
                    SELECT 
                        signal_type,
                        COUNT(*) as total_signals,
                        COUNT(CASE WHEN is_win = true THEN 1 END) as wins,
                        COUNT(CASE WHEN is_win = false THEN 1 END) as losses,
                        COUNT(CASE WHEN is_win IS NULL AND close_reason = 'timeout' THEN 1 END) as timeouts,
                        AVG(pnl_percent) as avg_pnl_pct,
                        SUM(pnl_usd) as total_pnl,
                        AVG(CASE WHEN is_win = true THEN pnl_usd END) as avg_win_profit,
                        AVG(CASE WHEN is_win = false THEN pnl_usd END) as avg_loss,
                        MAX(pnl_usd) as max_profit,
                        MIN(pnl_usd) as max_loss,
                        AVG(max_potential_profit_percent) as avg_max_potential_pct,
                        AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close
                    FROM web.scoring_history_results_v2
                    WHERE processed_at >= NOW() - INTERVAL '1 day'
                    GROUP BY signal_type
                ),
                combined AS (
                    SELECT 
                        'COMBINED' as signal_type,
                        COUNT(*) as total_signals,
                        COUNT(CASE WHEN is_win = true THEN 1 END) as wins,
                        COUNT(CASE WHEN is_win = false THEN 1 END) as losses,
                        COUNT(CASE WHEN is_win IS NULL AND close_reason = 'timeout' THEN 1 END) as timeouts,
                        AVG(pnl_percent) as avg_pnl_pct,
                        SUM(pnl_usd) as total_pnl,
                        AVG(CASE WHEN is_win = true THEN pnl_usd END) as avg_win_profit,
                        AVG(CASE WHEN is_win = false THEN pnl_usd END) as avg_loss,
                        MAX(pnl_usd) as max_profit,
                        MIN(pnl_usd) as max_loss,
                        AVG(max_potential_profit_percent) as avg_max_potential_pct,
                        AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close
                    FROM web.scoring_history_results_v2
                    WHERE processed_at >= NOW() - INTERVAL '1 day'
                )
                SELECT * FROM stats
                UNION ALL
                SELECT * FROM combined
                ORDER BY signal_type
            """

            with self.conn.cursor() as cur:
                cur.execute(stats_query)
                stats = cur.fetchall()

            logger.info("=" * 80)
            logger.info("📊 СРАВНИТЕЛЬНАЯ СТАТИСТИКА LONG vs SHORT (последние 24 часа):")
            logger.info("=" * 80)

            for stat in stats:
                if stat and stat['total_signals'] > 0:
                    signal_type = stat['signal_type']

                    if signal_type == 'COMBINED':
                        logger.info("\n" + "─" * 40)
                        logger.info("📈 ОБЩАЯ СТАТИСТИКА (LONG + SHORT):")
                    else:
                        logger.info(f"\n📊 {signal_type} ПОЗИЦИИ:")

                    logger.info(f"   ├─ Всего сигналов: {stat['total_signals']}")
                    logger.info(f"   ├─ Победы (TP): {stat['wins']}")
                    logger.info(f"   ├─ Поражения (SL): {stat['losses']}")
                    logger.info(f"   └─ Таймауты: {stat['timeouts']}")

                    if stat['wins'] and stat['losses']:
                        win_rate = stat['wins'] / (stat['wins'] + stat['losses']) * 100
                        logger.info(f"\n🏆 Win Rate: {win_rate:.1f}%")

                    if stat['avg_pnl_pct'] is not None:
                        logger.info(f"\n💰 Финансовые результаты:")
                        logger.info(f"   ├─ Средний P&L: {stat['avg_pnl_pct']:.2f}%")
                        logger.info(f"   ├─ Общий P&L: ${stat['total_pnl']:.2f}" if stat[
                            'total_pnl'] else "   ├─ Общий P&L: $0.00")
                        logger.info(f"   ├─ Средний профит: ${stat['avg_win_profit']:.2f}" if stat[
                            'avg_win_profit'] else "   ├─ Средний профит: N/A")
                        logger.info(f"   └─ Средний убыток: ${stat['avg_loss']:.2f}" if stat[
                            'avg_loss'] else "   └─ Средний убыток: N/A")

                    if stat['avg_max_potential_pct']:
                        logger.info(f"\n🚀 Средний максимальный потенциал: {stat['avg_max_potential_pct']:.2f}%")

                    if stat['avg_hours_to_close']:
                        logger.info(f"⏱️ Среднее время до закрытия: {stat['avg_hours_to_close']:.1f} часов")

            # Дополнительная статистика - какое направление лучше
            logger.info("\n" + "=" * 80)
            logger.info("🎯 РЕКОМЕНДАЦИИ ПО НАПРАВЛЕНИЯМ:")
            logger.info("=" * 80)

            comparison_query = """
                WITH direction_stats AS (
                    SELECT 
                        signal_type,
                        COUNT(CASE WHEN is_win = true THEN 1 END)::FLOAT / 
                            NULLIF(COUNT(CASE WHEN is_win IS NOT NULL THEN 1 END), 0) as win_rate,
                        AVG(pnl_percent) as avg_pnl_pct,
                        SUM(pnl_usd) as total_pnl
                    FROM web.scoring_history_results_v2
                    WHERE processed_at >= NOW() - INTERVAL '1 day'
                    GROUP BY signal_type
                )
                SELECT * FROM direction_stats
            """

            with self.conn.cursor() as cur:
                cur.execute(comparison_query)
                comparisons = cur.fetchall()

            best_winrate = None
            best_pnl = None

            for comp in comparisons:
                if comp['win_rate']:
                    if not best_winrate or comp['win_rate'] > best_winrate['win_rate']:
                        best_winrate = comp
                if comp['avg_pnl_pct']:
                    if not best_pnl or comp['avg_pnl_pct'] > best_pnl['avg_pnl_pct']:
                        best_pnl = comp

            if best_winrate:
                logger.info(f"✅ Лучший Win Rate: {best_winrate['signal_type']} ({best_winrate['win_rate'] * 100:.1f}%)")
            if best_pnl:
                logger.info(f"💰 Лучший средний P&L: {best_pnl['signal_type']} ({best_pnl['avg_pnl_pct']:.2f}%)")

            logger.info("=" * 80)

        except Exception as e:
            logger.error(f"❌ Ошибка при выводе статистики: {e}")

    def run(self):
        """Основной процесс анализа"""
        start_time = datetime.now()
        logger.info("🚀 Начало анализа исторических данных скоринга (v4.0)")
        logger.info(f"📅 Время запуска: {start_time.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        logger.info("✨ Расчет результатов одновременно для LONG и SHORT позиций")

        try:
            self.connect()

            batch_number = 0
            total_processed_in_run = 0

            while True:
                batch_number += 1

                signals = self.get_unprocessed_signals(self.config.batch_size)

                if not signals:
                    if batch_number == 1:
                        logger.info("✅ Нет новых сигналов для обработки")
                    else:
                        logger.info("✅ Все сигналы обработаны!")
                    break

                logger.info(f"\n📦 Обработка пакета #{batch_number}")
                logger.info(f"📊 В пакете: {len(signals)} сигналов")

                results = []
                batch_processed = 0

                for i, signal in enumerate(signals):
                    if i % 100 == 0 and i > 0:
                        progress = (i / len(signals)) * 100
                        logger.info(f"⏳ Пакет #{batch_number}: {i}/{len(signals)} ({progress:.1f}%)")

                    # Анализируем сигнал для обоих направлений
                    long_result, short_result = self.analyze_signal_both_directions(signal)

                    # Сохраняем результаты даже если это записи NO_DATA
                    if long_result and short_result:
                        results.append(long_result)
                        results.append(short_result)
                        self.processed_count += 1
                        batch_processed += 1

                    # Сохраняем результаты батчами
                    if len(results) >= self.config.save_batch_size:
                        self.save_results(results)
                        results = []

                # Сохраняем оставшиеся результаты
                if results:
                    self.save_results(results)

                total_processed_in_run += batch_processed
                logger.info(f"✅ Пакет #{batch_number} обработан: {batch_processed} сигналов")

                # Небольшая пауза между пакетами
                if batch_processed > 0:
                    time.sleep(1)

            end_time = datetime.now()
            duration = (end_time - start_time).total_seconds()

            logger.info("\n" + "=" * 80)
            logger.info("📋 ИТОГИ ОБРАБОТКИ:")
            logger.info("=" * 80)
            logger.info(f"✅ Успешно обработано сигналов: {self.processed_count}")
            logger.info(f"💾 Сохранено результатов: {self.new_signals_count} (по 2 на каждый сигнал)")
            logger.info(f"⭕ Пропущено (нет данных): {self.skipped_count}")
            logger.info(f"❌ Ошибок: {self.error_count}")
            logger.info(f"⏱️ Время выполнения: {duration:.1f} секунд ({duration / 60:.1f} минут)")

            if self.processed_count > 0:
                logger.info(f"⚡ Скорость обработки: {self.processed_count / duration:.1f} сигналов/сек")

            logger.info("=" * 80)

            # Выводим сравнительную статистику
            self.print_comparative_statistics()

        except Exception as e:
            logger.error(f"❌ Критическая ошибка: {e}")
            raise
        finally:
            self.disconnect()


def main():
    """Точка входа"""
    try:
        analyzer = ImprovedScoringAnalyzer()
        analyzer.run()
    except KeyboardInterrupt:
        logger.info("\n⛔ Прерывание пользователем")
        sys.exit(0)
    except Exception as e:
        logger.error(f"❌ Фатальная ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()