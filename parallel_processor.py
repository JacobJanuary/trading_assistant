"""
Модуль для параллельной обработки расчетов стратегий
Использует multiprocessing для эффективного использования всех CPU
"""
import multiprocessing as mp
import psycopg
import os
import logging
from typing import List, Dict, Tuple, Optional, Any
from datetime import datetime, timedelta
from dataclasses import dataclass
from concurrent.futures import ProcessPoolExecutor, as_completed
import numpy as np
from functools import partial
import time


# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class TradingConfig:
    """Конфигурация торговых параметров"""
    tp_percent: float = 4.0
    sl_percent: float = 3.0
    position_size: float = 100.0
    leverage: int = 5
    use_trailing_stop: bool = False
    trailing_distance_pct: float = 2.0
    trailing_activation_pct: float = 1.0


@dataclass
class TradeResult:
    """Результат торговой операции"""
    is_closed: bool
    close_reason: Optional[str]
    close_price: Optional[float]
    close_time: Optional[datetime]
    hours_to_close: Optional[float]
    is_win: Optional[bool]
    pnl_usd: float
    absolute_max_price: float
    absolute_min_price: float
    time_to_max: float
    time_to_min: float
    max_drawdown_from_peak: float


class DatabaseConnectionManager:
    """Менеджер подключений к БД для worker процессов"""
    
    def __init__(self, database_url: str):
        self.database_url = database_url
        
    def get_connection(self):
        """Создает новое подключение для worker процесса"""
        return psycopg.connect(self.database_url, autocommit=False)


def calculate_trade_result_worker(direction: str, entry_price: float, 
                                history: List[Dict], actual_entry_time: datetime,
                                config: TradingConfig) -> TradeResult:
    """
    Worker функция для расчета результата торговли
    Оптимизирована для выполнения в отдельном процессе
    """
    tp_percent = config.tp_percent
    sl_percent = config.sl_percent
    position_size = config.position_size
    leverage = config.leverage
    
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
                sl_hit = low_price <= sl_price
                tp_hit = high_price >= tp_price
                
                if sl_hit and tp_hit:
                    # Консервативный подход - SL первый
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
                
                # Обновляем running_best_price для LONG
                if high_price > running_best_price:
                    running_best_price = high_price
                
                # Расчет drawdown от пика
                current_drawdown = (running_best_price - low_price) / running_best_price * 100
                if current_drawdown > max_drawdown_from_peak:
                    max_drawdown_from_peak = current_drawdown
                    
            else:  # SHORT
                sl_hit = high_price >= sl_price
                tp_hit = low_price <= tp_price
                
                if sl_hit and tp_hit:
                    # Консервативный подход - SL первый
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
                
                # Обновляем running_best_price для SHORT
                if low_price < running_best_price:
                    running_best_price = low_price
                
                # Расчет drawdown от пика для SHORT
                current_drawdown = (high_price - running_best_price) / running_best_price * 100
                if current_drawdown > max_drawdown_from_peak:
                    max_drawdown_from_peak = current_drawdown
        
        # Проверяем таймаут (48 часов)
        if hours_passed >= 48 and not is_closed:
            is_closed = True
            close_reason = 'timeout'
            is_win = False
            close_price = float(candle['close_price'])
            close_time = current_time
            hours_to_close = hours_passed
            break
    
    # Если позиция так и не закрылась
    if not is_closed and history:
        last_candle = history[-1]
        close_price = float(last_candle['close_price'])
        close_time = last_candle['timestamp']
        hours_to_close = (close_time - actual_entry_time).total_seconds() / 3600
        close_reason = 'still_open'
        is_win = False
    
    # Расчет P&L
    pnl_usd = 0
    if close_price and is_closed:
        if direction == 'LONG':
            price_change_percent = (close_price - entry_price) / entry_price * 100
        else:  # SHORT
            price_change_percent = (entry_price - close_price) / entry_price * 100
        
        pnl_usd = position_size * leverage * (price_change_percent / 100)
    
    return TradeResult(
        is_closed=is_closed,
        close_reason=close_reason,
        close_price=close_price,
        close_time=close_time,
        hours_to_close=hours_to_close,
        is_win=is_win,
        pnl_usd=pnl_usd,
        absolute_max_price=absolute_max_price,
        absolute_min_price=absolute_min_price,
        time_to_max=time_to_max,
        time_to_min=time_to_min,
        max_drawdown_from_peak=max_drawdown_from_peak
    )


def process_signal_worker(signal_data: Tuple[Dict, str]) -> Tuple[Optional[Dict], Optional[Dict], str]:
    """
    Worker функция для обработки одного сигнала
    Возвращает результаты для LONG и SHORT направлений
    """
    signal, database_url = signal_data
    
    try:
        # Создаем подключение к БД в worker процессе
        conn = psycopg.connect(database_url, autocommit=False)
        
        config = TradingConfig()
        
        # Получаем цену входа
        entry_price_query = """
            SELECT 
                open_price as entry_price,
                timestamp
            FROM fas.market_data_aggregated
            WHERE trading_pair_id = %s
                AND timeframe = '15m'
                AND timestamp >= %s - INTERVAL '15 minutes'
                AND timestamp <= %s + INTERVAL '15 minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
            LIMIT 1
        """
        
        with conn.cursor() as cur:
            cur.execute(entry_price_query, (
                signal['trading_pair_id'], 
                signal['timestamp'],
                signal['timestamp'], 
                signal['timestamp']
            ))
            price_result = cur.fetchone()
        
        if not price_result:
            # Расширенный поиск
            extended_query = """
                SELECT 
                    open_price as entry_price,
                    timestamp
                FROM fas.market_data_aggregated
                WHERE trading_pair_id = %s
                    AND timeframe = '15m'
                    AND timestamp >= %s - INTERVAL '1 hour'
                    AND timestamp <= %s + INTERVAL '1 hour'
                ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
                LIMIT 1
            """
            with conn.cursor() as cur:
                cur.execute(extended_query, (
                    signal['trading_pair_id'], 
                    signal['timestamp'],
                    signal['timestamp'], 
                    signal['timestamp']
                ))
                price_result = cur.fetchone()
        
        if not price_result:
            conn.close()
            return None, None, f"No entry price for {signal.get('pair_symbol', 'unknown')}"
        
        entry_price = float(price_result[0])
        actual_entry_time = price_result[1]
        
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
        
        with conn.cursor() as cur:
            cur.execute(history_query, (
                signal['trading_pair_id'],
                actual_entry_time,
                actual_entry_time
            ))
            history = cur.fetchall()
        
        conn.close()
        
        if not history or len(history) < 10:
            return None, None, f"Insufficient history for {signal.get('pair_symbol', 'unknown')}"
        
        # Рассчитываем результаты для обоих направлений
        long_result = calculate_trade_result_worker(
            'LONG', entry_price, history, actual_entry_time, config
        )
        
        short_result = calculate_trade_result_worker(
            'SHORT', entry_price, history, actual_entry_time, config
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
            'tp_percent': config.tp_percent,
            'sl_percent': config.sl_percent,
            'position_size': config.position_size,
            'leverage': config.leverage,
            'analysis_end_time': actual_entry_time + timedelta(hours=48)
        }
        
        # Результат для LONG
        long_data = {**base_data}
        long_data.update({
            'signal_type': 'LONG',
            'entry_price': entry_price,
            'actual_entry_time': actual_entry_time,
            'is_closed': long_result.is_closed,
            'close_reason': long_result.close_reason,
            'close_price': long_result.close_price,
            'close_time': long_result.close_time,
            'hours_to_close': long_result.hours_to_close,
            'is_win': long_result.is_win,
            'pnl_usd': long_result.pnl_usd,
            'absolute_max_price': long_result.absolute_max_price,
            'absolute_min_price': long_result.absolute_min_price,
            'time_to_max_hours': long_result.time_to_max,
            'time_to_min_hours': long_result.time_to_min,
            'max_drawdown_from_peak': long_result.max_drawdown_from_peak
        })
        
        # Результат для SHORT
        short_data = {**base_data}
        short_data.update({
            'signal_type': 'SHORT',
            'entry_price': entry_price,
            'actual_entry_time': actual_entry_time,
            'is_closed': short_result.is_closed,
            'close_reason': short_result.close_reason,
            'close_price': short_result.close_price,
            'close_time': short_result.close_time,
            'hours_to_close': short_result.hours_to_close,
            'is_win': short_result.is_win,
            'pnl_usd': short_result.pnl_usd,
            'absolute_max_price': short_result.absolute_max_price,
            'absolute_min_price': short_result.absolute_min_price,
            'time_to_max_hours': short_result.time_to_max,
            'time_to_min_hours': short_result.time_to_min,
            'max_drawdown_from_peak': short_result.max_drawdown_from_peak
        })
        
        return long_data, short_data, "success"
        
    except Exception as e:
        return None, None, f"Error processing signal: {str(e)}"


class ParallelSignalProcessor:
    """
    Основной класс для параллельной обработки сигналов
    """
    
    def __init__(self, database_url: str, max_workers: Optional[int] = None):
        self.database_url = database_url
        
        # Используем количество CPU, но не больше 14 (оставляем 2 для системы)
        cpu_count = mp.cpu_count()
        self.max_workers = max_workers or min(cpu_count - 2, 14)
        
        logger.info(f"Инициализация параллельного процессора с {self.max_workers} workers")
    
    def process_signals_parallel(self, signals: List[Dict], 
                               session_id: str, user_id: int) -> Dict[str, Any]:
        """
        Параллельная обработка списка сигналов
        """
        start_time = time.time()
        total_signals = len(signals)
        
        logger.info(f"Начинаем параллельную обработку {total_signals} сигналов")
        logger.info(f"Используем {self.max_workers} worker процессов")
        
        # Подготавливаем данные для worker'ов
        signal_data = [(signal, self.database_url) for signal in signals]
        
        # Статистика
        processed_count = 0
        error_count = 0
        batch_data = []
        
        # Обрабатываем сигналы параллельно
        with ProcessPoolExecutor(max_workers=self.max_workers) as executor:
            # Отправляем все задачи
            futures = {
                executor.submit(process_signal_worker, data): i 
                for i, data in enumerate(signal_data)
            }
            
            # Собираем результаты по мере готовности
            for future in as_completed(futures):
                signal_index = futures[future]
                
                try:
                    long_result, short_result, status = future.result()
                    
                    if status == "success" and long_result and short_result:
                        batch_data.append(long_result)
                        batch_data.append(short_result)
                        processed_count += 1
                    else:
                        error_count += 1
                        logger.warning(f"Signal {signal_index}: {status}")
                    
                    # Логируем прогресс каждые 100 обработанных сигналов
                    if (processed_count + error_count) % 100 == 0:
                        progress = ((processed_count + error_count) / total_signals) * 100
                        elapsed = time.time() - start_time
                        logger.info(f"Прогресс: {processed_count + error_count}/{total_signals} "
                                  f"({progress:.1f}%) за {elapsed:.1f}с")
                        
                except Exception as e:
                    error_count += 1
                    logger.error(f"Ошибка обработки сигнала {signal_index}: {e}")
        
        # Сохраняем результаты в БД батчами
        if batch_data:
            self._save_results_batch(batch_data, session_id, user_id)
        
        elapsed_time = time.time() - start_time
        
        result = {
            'processed_count': processed_count,
            'error_count': error_count,
            'total_results': len(batch_data),
            'elapsed_time': elapsed_time,
            'signals_per_second': total_signals / elapsed_time if elapsed_time > 0 else 0
        }
        
        logger.info(f"Обработка завершена за {elapsed_time:.1f}с")
        logger.info(f"Обработано: {processed_count}, ошибок: {error_count}")
        logger.info(f"Скорость: {result['signals_per_second']:.1f} сигналов/сек")
        
        return result
    
    def _save_results_batch(self, batch_data: List[Dict], session_id: str, user_id: int):
        """
        Сохранение результатов в БД батчами
        """
        if not batch_data:
            return
        
        logger.info(f"Сохраняем {len(batch_data)} результатов в БД...")
        
        # Очищаем предыдущие результаты для этой сессии
        clear_query = """
            DELETE FROM web.scoring_analysis_results 
            WHERE session_id = %s AND user_id = %s
        """
        
        # SQL для вставки
        insert_query = """
            INSERT INTO web.scoring_analysis_results (
                session_id, user_id, scoring_history_id, signal_timestamp,
                pair_symbol, trading_pair_id, market_regime, total_score,
                indicator_score, pattern_score, combination_score,
                signal_type, entry_price, actual_entry_time, is_closed,
                close_reason, close_price, close_time, hours_to_close,
                is_win, pnl_usd, absolute_max_price, absolute_min_price,
                time_to_max_hours, time_to_min_hours, max_drawdown_from_peak,
                tp_percent, sl_percent, position_size, leverage, analysis_end_time
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """
        
        try:
            conn = psycopg.connect(self.database_url, autocommit=False)
            
            with conn.cursor() as cur:
                # Очищаем предыдущие результаты
                cur.execute(clear_query, (session_id, user_id))
                
                # Вставляем новые результаты
                for result in batch_data:
                    values = (
                        session_id, user_id, result['scoring_history_id'],
                        result['signal_timestamp'], result['pair_symbol'],
                        result['trading_pair_id'], result['market_regime'],
                        result['total_score'], result['indicator_score'],
                        result['pattern_score'], result['combination_score'],
                        result['signal_type'], result['entry_price'],
                        result['actual_entry_time'], result['is_closed'],
                        result['close_reason'], result['close_price'],
                        result['close_time'], result['hours_to_close'],
                        result['is_win'], result['pnl_usd'],
                        result['absolute_max_price'], result['absolute_min_price'],
                        result['time_to_max_hours'], result['time_to_min_hours'],
                        result['max_drawdown_from_peak'], result['tp_percent'],
                        result['sl_percent'], result['position_size'],
                        result['leverage'], result['analysis_end_time']
                    )
                    cur.execute(insert_query, values)
                
                conn.commit()
                logger.info(f"Успешно сохранено {len(batch_data)} результатов")
                
        except Exception as e:
            logger.error(f"Ошибка сохранения результатов: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()


def get_optimal_worker_count() -> int:
    """
    Определяет оптимальное количество worker процессов
    """
    cpu_count = mp.cpu_count()
    # Оставляем 2 CPU для системы и основного процесса
    optimal_count = max(1, cpu_count - 2)
    
    # Ограничиваем максимум до 14 процессов для стабильности
    return min(optimal_count, 14)


# Функция для замены существующей process_scoring_signals_batch
def process_scoring_signals_batch_parallel(db, signals, session_id, user_id,
                                         tp_percent=4.0, sl_percent=3.0,
                                         position_size=100.0, leverage=5,
                                         use_trailing_stop=False,
                                         trailing_distance_pct=2.0,
                                         trailing_activation_pct=1.0):
    """
    Параллельная версия process_scoring_signals_batch
    """
    # Получаем database_url из объекта Database
    database_url = db.database_url
    
    processor = ParallelSignalProcessor(database_url)
    
    return processor.process_signals_parallel(signals, session_id, user_id)