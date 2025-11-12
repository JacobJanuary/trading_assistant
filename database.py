"""
Модуль для работы с базой данных PostgreSQL
Работает с существующей таблицей large_trades
Использует psycopg3
"""
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout
import os
from contextlib import contextmanager
import logging
import time
from typing import Optional
from datetime import datetime, timezone
from config import Config

# Константа для slippage на stop-loss
SLIPPAGE_PERCENT = 0.05  # 0.05% проскальзывание на stop-loss


def make_aware(dt):
    """Преобразует naive datetime в aware (UTC)"""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        # Если naive, добавляем UTC timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


# ============================================
# CANDLE DATA ABSTRACTION LAYER
# ============================================
# Helper functions для поддержки миграции с fas_v2.market_data_aggregated на public.candles
# Используют Config.USE_PUBLIC_CANDLES для выбора источника данных


def convert_timestamp_param(ts):
    """
    Конвертирует timestamp параметр в нужный формат для текущей таблицы свечей

    Args:
        ts: datetime object или timestamp with timezone

    Returns:
        int: миллисекунды (для public.candles) или datetime (для legacy)
    """
    from datetime import datetime

    _, cols = get_candle_table_info()

    if cols.get('use_unix_ms'):
        # public.candles: конвертируем в миллисекунды
        if isinstance(ts, datetime):
            return int(ts.timestamp() * 1000)
        elif isinstance(ts, (int, float)):
            return int(ts * 1000) if ts < 10000000000 else int(ts)  # Если уже в ms
        else:
            raise ValueError(f"Unsupported timestamp type: {type(ts)}")
    else:
        # fas_v2.market_data_aggregated: возвращаем как есть
        return ts


def get_candle_table_info():
    """
    Возвращает информацию о таблице свечей и маппинг колонок
    для поддержки миграции fas_v2.market_data_aggregated → public.candles

    Returns:
        tuple: (table_name: str, column_aliases: dict)

    Колонки в aliases:
        - open: SELECT фрагмент для open_price
        - high: SELECT фрагмент для high_price
        - low: SELECT фрагмент для low_price
        - close: SELECT фрагмент для close_price
        - timestamp, trading_pair_id, timeframe: AS IS

    Example:
        >>> table, aliases = get_candle_table_info()
        >>> print(f"SELECT {aliases['open']} FROM {table}")
        SELECT open AS open_price FROM public.candles
    """
    if Config.USE_PUBLIC_CANDLES:
        # public.candles: РЕАЛЬНАЯ структура (проверено 2025-11-06 23:50)
        # - Колонки УЖЕ имеют суффикс _price (как в fas_v2!)
        # - open_time вместо timestamp (BIGINT Unix ms) - сравниваем НАПРЯМУЮ!
        # - interval_id вместо timeframe (1=5m, 2=15m, 3=1h, 4=4h, 5=24h)
        return "public.candles", {
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'close': 'close_price',
            'timestamp': 'open_time',  # BIGINT миллисекунды (для WHERE прямое сравнение)
            'timestamp_display': 'to_timestamp(open_time / 1000)',  # Для SELECT (отображение)
            'trading_pair_id': 'trading_pair_id',
            'timeframe': 'interval_id',  # Используем напрямую (1 = '5m')
            'timeframe_value': '1',  # Значение для WHERE (interval_id = 1 для 5m)
            'use_unix_ms': True  # Флаг что используем Unix миллисекунды
        }
    else:
        # fas_v2.market_data_aggregated: legacy таблица
        return "fas_v2.market_data_aggregated", {
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'close': 'close_price',
            'timestamp': 'timestamp',
            'timestamp_display': 'timestamp',
            'trading_pair_id': 'trading_pair_id',
            'timeframe': 'timeframe',
            'timeframe_value': "'5m'",  # Значение для WHERE (timeframe = '5m')
            'use_unix_ms': False  # Флаг что используем TIMESTAMP
        }


def build_entry_price_query(window_minutes=5):
    """
    Строит SQL запрос для получения цены входа с автовыбором таблицы

    Args:
        window_minutes: Окно поиска в минутах (default: 5)

    Returns:
        str: SQL query для получения entry price

    Example:
        >>> query = build_entry_price_query(5)
        >>> result = db.execute_query(query, (pair_id, ts_ms, ts_ms, ts_ms))  # ts_ms = timestamp * 1000
        >>> entry_price = result[0]['entry_price']
    """
    table, cols = get_candle_table_info()

    if cols.get('use_unix_ms'):
        # public.candles: сравниваем open_time (BIGINT ms) напрямую
        # Параметры должны быть в миллисекундах: EXTRACT(EPOCH FROM timestamp) * 1000
        window_ms = window_minutes * 60 * 1000
        return f"""
            SELECT {cols['open']} as entry_price
            FROM {table}
            WHERE {cols['trading_pair_id']} = %s
                AND {cols['timeframe']} = {cols['timeframe_value']}
                AND {cols['timestamp']} >= %s - {window_ms}
                AND {cols['timestamp']} <= %s + {window_ms}
            ORDER BY ABS({cols['timestamp']} - %s) ASC
            LIMIT 1
        """
    else:
        # fas_v2.market_data_aggregated: используем INTERVAL с timestamp
        return f"""
            SELECT {cols['open']} as entry_price
            FROM {table}
            WHERE {cols['trading_pair_id']} = %s
                AND {cols['timeframe']} = {cols['timeframe_value']}
                AND {cols['timestamp']} >= %s - INTERVAL '{window_minutes} minutes'
                AND {cols['timestamp']} <= %s + INTERVAL '{window_minutes} minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM ({cols['timestamp']} - %s))) ASC
            LIMIT 1
        """


def build_entry_price_fallback_query(window_hours=1):
    """
    Строит SQL запрос для fallback поиска цены входа (расширенное окно)

    Args:
        window_hours: Расширенное окно поиска в часах (default: 1)

    Returns:
        str: SQL query для fallback поиска entry price

    Example:
        >>> query = build_entry_price_fallback_query(1)
        >>> result = db.execute_query(query, (pair_id, ts_ms, ts_ms, ts_ms))
    """
    table, cols = get_candle_table_info()

    if cols.get('use_unix_ms'):
        # public.candles: сравниваем open_time (BIGINT ms) напрямую
        window_ms = window_hours * 60 * 60 * 1000
        return f"""
            SELECT {cols['open']} as entry_price
            FROM {table}
            WHERE {cols['trading_pair_id']} = %s
                AND {cols['timeframe']} = {cols['timeframe_value']}
                AND {cols['timestamp']} >= %s - {window_ms}
                AND {cols['timestamp']} <= %s + {window_ms}
            ORDER BY ABS({cols['timestamp']} - %s) ASC
            LIMIT 1
        """
    else:
        # fas_v2.market_data_aggregated: используем INTERVAL с timestamp
        return f"""
            SELECT {cols['open']} as entry_price
            FROM {table}
            WHERE {cols['trading_pair_id']} = %s
                AND {cols['timeframe']} = {cols['timeframe_value']}
                AND {cols['timestamp']} >= %s - INTERVAL '{window_hours} hour'
                AND {cols['timestamp']} <= %s + INTERVAL '{window_hours} hour'
            ORDER BY ABS(EXTRACT(EPOCH FROM ({cols['timestamp']} - %s))) ASC
            LIMIT 1
        """


def build_candle_history_query(duration_hours=24):
    """
    Строит SQL запрос для получения истории свечей

    Args:
        duration_hours: Длительность истории в часах (default: 24)

    Returns:
        str: SQL query для получения истории свечей

    Example:
        >>> query = build_candle_history_query(24)
        >>> history = db.execute_query(query, (pair_id, start_ts_ms, end_ts_ms))
        >>> for candle in history:
        >>>     print(candle['open_price'], candle['high_price'])
    """
    table, cols = get_candle_table_info()

    if cols.get('use_unix_ms'):
        # public.candles: сравниваем open_time (BIGINT ms) напрямую
        # Для SELECT используем timestamp_display для отображения
        return f"""
            SELECT
                {cols['timestamp_display']} as timestamp,
                {cols['open']},
                {cols['high']},
                {cols['low']},
                {cols['close']}
            FROM {table}
            WHERE {cols['trading_pair_id']} = %s
                AND {cols['timeframe']} = {cols['timeframe_value']}
                AND {cols['timestamp']} >= %s
                AND {cols['timestamp']} <= %s
            ORDER BY {cols['timestamp']} ASC
        """
    else:
        # fas_v2.market_data_aggregated: используем INTERVAL с timestamp
        return f"""
            SELECT
                {cols['timestamp']},
                {cols['open']},
                {cols['high']},
                {cols['low']},
                {cols['close']}
            FROM {table}
            WHERE {cols['trading_pair_id']} = %s
                AND {cols['timeframe']} = {cols['timeframe_value']}
                AND {cols['timestamp']} >= %s
                AND {cols['timestamp']} <= %s + INTERVAL '{duration_hours} hours'
            ORDER BY {cols['timestamp']} ASC
        """


# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Database:
    """Класс для управления подключениями к базе данных"""
    
    # Счетчик ошибок соединения для автоматической переинициализации
    _connection_error_count = 0
    _last_error_reset = time.time()
    _max_errors_before_reinit = Config.DB_MAX_ERRORS_BEFORE_REINIT
    _error_reset_interval = Config.DB_ERROR_RESET_INTERVAL

    def __init__(self, host=None, port=None, database=None, user=None, password=None, database_url=None, use_pool=True):
        """
        Инициализация базы данных. Можно передать либо отдельные параметры, либо database_url.
        
        Args:
            host: хост базы данных
            port: порт базы данных
            database: имя базы данных
            user: имя пользователя
            password: пароль (опционально, если используется .pgpass)
            database_url: полная строка подключения (используется если не переданы отдельные параметры)
            use_pool: использовать пул подключений (по умолчанию True)
        """
        # Для периодической проверки здоровья пула
        self._last_pool_check = time.time()
        self._pool_check_interval = 60  # Проверяем пул каждые 60 секунд
        if database_url:
            self.database_url = database_url
        else:
            # Формируем строку подключения из отдельных параметров в формате key=value
            # Если пароль не указан, psycopg автоматически использует .pgpass
            # Формируем строку подключения с параметрами для стабильности
            params = [
                f"host={host}",
                f"port={port or 5432}",
                f"dbname={database}",
                f"user={user}"
            ]
            
            if password:
                params.append(f"password={password}")
            
            # Добавляем параметры для стабильности соединения из конфигурации
            params.extend([
                "sslmode=disable",  # Отключаем SSL для стабильности
                f"connect_timeout={Config.DB_CONNECT_TIMEOUT}",
                f"keepalives={Config.DB_KEEPALIVES}",
                f"keepalives_idle={Config.DB_KEEPALIVES_IDLE}",
                f"keepalives_interval={Config.DB_KEEPALIVES_INTERVAL}",
                f"keepalives_count={Config.DB_KEEPALIVES_COUNT}",
                f"tcp_user_timeout={Config.DB_TCP_USER_TIMEOUT}"
            ])
            
            self.database_url = " ".join(params)

        self.use_pool = use_pool
        self.connection_pool = None
        if use_pool:
            self._initialize_pool()

    @staticmethod
    def _check_connection(conn):
        """Проверка соединения перед выдачей из пула"""
        try:
            # Проверяем что соединение не закрыто
            if conn.closed:
                logger.debug("Connection is closed, will be replaced")
                raise psycopg.OperationalError("Connection is closed")
            
            # Проверяем состояние соединения более тщательно
            if hasattr(conn, 'info') and hasattr(conn.info, 'transaction_status'):
                tx_status = conn.info.transaction_status
                
                # Если соединение в ошибочном состоянии, пытаемся восстановить
                if tx_status == psycopg.pq.TransactionStatus.INERROR:
                    logger.debug("Connection in INERROR state, rolling back")
                    try:
                        conn.rollback()
                    except:
                        # Если rollback не удался, соединение битое
                        raise psycopg.OperationalError("Failed to rollback INERROR connection")
                
                # Если соединение в транзакции - откатываем (не должно быть возвращенных соединений в транзакции)
                elif tx_status == psycopg.pq.TransactionStatus.INTRANS:
                    logger.warning("Connection in INTRANS state, rolling back")
                    try:
                        conn.rollback()
                    except:
                        raise psycopg.OperationalError("Failed to rollback INTRANS connection")
                
                # Если соединение в неизвестном состоянии
                elif tx_status == psycopg.pq.TransactionStatus.UNKNOWN:
                    logger.warning("Connection in UNKNOWN state")
                    raise psycopg.OperationalError("Connection in UNKNOWN state")
            
            # КРИТИЧНО: Устанавливаем короткий таймаут для проверки
            conn.execute("SET statement_timeout = '2s'")
            # Устанавливаем TCP keepalive только для этого соединения
            conn.execute("SET tcp_keepalives_idle = 30")
            conn.execute("SET tcp_keepalives_interval = 10")
            conn.execute("SET tcp_keepalives_count = 3")
            
            # Быстрая проверка работоспособности
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT 1", prepare=False)  # Не используем prepared statement
                result = cur.fetchone()
                if not result or result[0] != 1:
                    raise psycopg.OperationalError("Health check query failed")
            
            # Возвращаем таймаут к стандартному значению
            conn.execute("SET statement_timeout = '30s'")
            
            return True
            
        except (psycopg.OperationalError, psycopg.InterfaceError, OSError) as e:
            # Соединение точно битое
            logger.warning(f"Connection check failed (will be discarded): {e}")
            raise
        except Exception as e:
            # Неожиданная ошибка
            logger.error(f"Unexpected error in connection check: {e}")
            raise psycopg.OperationalError(f"Connection check failed: {e}")
    
    def _initialize_pool(self):
        """Инициализация пула подключений"""
        try:
            # Базовые параметры, поддерживаемые всеми версиями
            # НЕ используем reset - он вызывает проблемы
            # Вместо этого используем check для проверки перед выдачей
            pool_params = {
                "conninfo": self.database_url,
                "min_size": Config.DB_POOL_MIN_SIZE,
                "max_size": Config.DB_POOL_MAX_SIZE,
                "timeout": Config.DB_POOL_TIMEOUT
                # НЕ используем reset - вызывает INERROR
            }
            
            # Добавляем дополнительные параметры если поддерживаются
            try:
                # Пробуем создать с расширенными параметрами
                # Добавляем check если поддерживается
                extended_params = pool_params.copy()
                extended_params.update({
                    "max_idle": Config.DB_POOL_MAX_IDLE,
                    "max_lifetime": Config.DB_POOL_MAX_LIFETIME,
                    "max_waiting": Config.DB_POOL_MAX_WAITING,
                    "check": self._check_connection  # Проверка соединения перед выдачей
                })
                self.connection_pool = ConnectionPool(**extended_params)
                logger.info("Пул подключений инициализирован с расширенными параметрами")
            except TypeError:
                # Если не поддерживаются, создаем с базовыми
                self.connection_pool = ConnectionPool(**pool_params)
                logger.info("Пул подключений инициализирован с базовыми параметрами")
                
        except Exception as e:
            logger.error(f"Ошибка при инициализации пула подключений: {e}")
            raise
    

    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для получения подключения"""
        connection = None
        
        # Периодическая проверка здоровья пула
        if self.use_pool and self.connection_pool:
            current_time = time.time()
            if current_time - self._last_pool_check > self._pool_check_interval:
                try:
                    self.check_pool_health()
                    self._last_pool_check = current_time
                except Exception as e:
                    logger.warning(f"Periodic pool health check failed: {e}")
        
        try:
            if self.use_pool:
                # Пытаемся получить соединение из пула с retry логикой
                retry_count = 0
                max_retries = Config.DB_MAX_RETRIES
                
                while retry_count < max_retries:
                    try:
                        connection = self.connection_pool.getconn()
                        break  # Успешно получили соединение
                    except PoolTimeout as e:
                        logger.warning(f"Pool timeout on attempt {retry_count + 1}/{max_retries}: {e}")
                        retry_count += 1
                        
                        if retry_count < max_retries:
                            # Проверяем здоровье пула
                            self.check_pool_health()
                            time.sleep(retry_count)  # Прогрессивная задержка
                        else:
                            logger.error("Failed to get connection from pool after retries")
                            raise
                
                # Если у нас есть функция check в пуле, она уже проверила соединение
                # Дополнительная проверка только если соединение явно закрыто
                if connection and connection.closed:
                    logger.error("Получено закрытое соединение из пула несмотря на check")
                    # Это критическая ситуация - переинициализируем пул
                    self._track_connection_error()
                    if self._connection_error_count >= self._max_errors_before_reinit:
                        self.reinitialize_pool()
                        self._connection_error_count = 0
                    connection = self.connection_pool.getconn()
                
                # Очищаем состояние соединения
                if connection:
                    # Обрабатываем различные состояния транзакций
                    tx_status = connection.info.transaction_status
                    
                    if tx_status == psycopg.pq.TransactionStatus.INTRANS:
                        # В активной транзакции - откатываем
                        logger.warning("Connection in INTRANS state, rolling back")
                        try:
                            connection.rollback()
                        except Exception as e:
                            logger.error(f"Failed to rollback: {e}")
                    elif tx_status == psycopg.pq.TransactionStatus.INERROR:
                        # В состоянии ошибки - откатываем
                        logger.warning("Connection in INERROR state, rolling back")
                        try:
                            connection.rollback()
                        except Exception as e:
                            logger.error(f"Failed to rollback error transaction: {e}")
                    
                    # КРИТИЧНО: Отключаем prepared statements для Gunicorn
                    # Это предотвращает конфликты между процессами
                    try:
                        connection.prepare_threshold = None
                    except AttributeError:
                        # Если атрибут не существует в этой версии psycopg
                        pass
                    
                    # Устанавливаем autocommit=False для транзакционности
                    connection.autocommit = False
            else:
                # Создаем новое подключение без пула
                connection = psycopg.connect(self.database_url, autocommit=False)
                # Отключаем prepared statements
                try:
                    connection.prepare_threshold = None
                except AttributeError:
                    pass
                # Устанавливаем агрессивные keepalive параметры для этого соединения
                connection.execute("SET tcp_keepalives_idle = 30")
                connection.execute("SET tcp_keepalives_interval = 10") 
                connection.execute("SET tcp_keepalives_count = 3")
            
            # Возвращаем соединение для использования
            yield connection
            
            # Успешное выполнение - сбрасываем счетчик ошибок
            self._connection_error_count = 0
            
        except psycopg.OperationalError as e:
            # Специальная обработка ошибок соединения
            logger.error(f"Ошибка соединения с базой данных: {e}")
            if connection:
                try:
                    connection.rollback()
                except:
                    pass
                # КРИТИЧНО: Закрываем битое соединение
                if self.use_pool:
                    try:
                        connection.close()  # Закрываем битое соединение
                        connection = None  # Помечаем что соединение обработано
                    except:
                        pass
            raise
            
        except Exception as e:
            # Общая обработка ошибок
            logger.error(f"Ошибка при работе с базой данных: {e}")
            if connection:
                try:
                    connection.rollback()
                except:
                    pass
            raise
            
        finally:
            # Возвращаем соединение в пул или закрываем
            if connection:
                if self.use_pool:
                    try:
                        # Проверяем состояние транзакции перед возвратом в пул
                        if not connection.closed:
                            tx_status = connection.info.transaction_status
                            
                            # Если соединение не в IDLE состоянии
                            if tx_status != psycopg.pq.TransactionStatus.IDLE:
                                logger.warning(f"Returning connection to pool with status: {tx_status}")
                                try:
                                    connection.rollback()
                                    # После rollback проверяем что соединение в IDLE
                                    if connection.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
                                        self.connection_pool.putconn(connection)
                                    else:
                                        # Если все еще не IDLE - закрываем
                                        connection.close()
                                except Exception as e:
                                    logger.error(f"Failed to rollback before returning to pool: {e}")
                                    # Если rollback не удался, закрываем соединение
                                    try:
                                        connection.close()
                                    except:
                                        pass
                            else:
                                # Только чистые соединения возвращаем в пул
                                self.connection_pool.putconn(connection)
                    except Exception as e:
                        logger.error(f"Ошибка при возврате соединения в пул: {e}")
                        try:
                            connection.close()
                        except:
                            pass
                else:
                    try:
                        connection.close()
                    except:
                        pass

    def _track_connection_error(self):
        """Отслеживание ошибок соединения для автоматической переинициализации"""
        current_time = time.time()
        
        # Сбрасываем счетчик, если прошло достаточно времени
        if current_time - self._last_error_reset > self._error_reset_interval:
            self._connection_error_count = 0
            self._last_error_reset = current_time
        
        # Увеличиваем счетчик
        self._connection_error_count += 1
        logger.info(f"Connection error count: {self._connection_error_count}/{self._max_errors_before_reinit}")
    
    def validate_connection(self, conn):
        """Валидация отдельного соединения"""
        try:
            if conn.closed:
                return False
            
            # Проверяем работоспособность
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT 1", prepare=False)
                result = cur.fetchone()
                return result and result[0] == 1
        except:
            return False
    
    def check_pool_health(self):
        """Проверка здоровья пула соединений с очисткой плохих соединений"""
        if not self.connection_pool:
            return False
        try:
            # Проверяем статистику пула
            stats = self.connection_pool.get_stats()
            logger.info(f"Pool stats: size={stats.get('pool_size', 0)}, "
                       f"available={stats.get('pool_available', 0)}, "
                       f"waiting={stats.get('requests_waiting', 0)}")
            
            # Если есть много ожидающих запросов, пробуем очистить пул
            if stats.get('requests_waiting', 0) > 5:
                logger.warning(f"High number of waiting requests: {stats.get('requests_waiting', 0)}")
                
                # Пробуем сбросить idle соединения
                try:
                    self.connection_pool.check()
                except:
                    pass
                    
            # Проверяем доступные соединения и закрываем плохие
            if hasattr(self.connection_pool, '_pool'):
                bad_connections = []
                for conn in list(self.connection_pool._pool):
                    if conn.closed:
                        bad_connections.append(conn)
                
                # Удаляем плохие соединения из пула
                for conn in bad_connections:
                    try:
                        self.connection_pool._pool.remove(conn)
                        logger.info("Removed bad connection from pool")
                    except:
                        pass
                
            return True
        except Exception as e:
            logger.error(f"Error checking pool health: {e}")
            return False
    
    def reinitialize_pool(self):
        """Полная переинициализация пула соединений при критических ошибках"""
        logger.warning("CRITICAL: Reinitializing entire connection pool")
        
        # Агрессивно закрываем все соединения
        if self.connection_pool:
            try:
                # Пытаемся закрыть все соединения в пуле
                if hasattr(self.connection_pool, '_pool'):
                    for conn in list(self.connection_pool._pool):
                        try:
                            conn.close()
                        except:
                            pass
                
                # Закрываем сам пул
                self.connection_pool.close()
                logger.info("Old pool forcefully closed")
            except Exception as e:
                logger.error(f"Error closing old pool: {e}")
            
            # Обнуляем ссылку на пул
            self.connection_pool = None
        
        # Пауза для освобождения ресурсов и сброса состояния на сервере БД
        time.sleep(2)
        
        # Создаем новый пул с нуля
        max_attempts = Config.DB_MAX_RETRIES
        for attempt in range(max_attempts):
            try:
                self._initialize_pool()
                
                # Проверяем что новый пул работает
                test_conn = self.connection_pool.getconn()
                with test_conn.cursor() as cur:
                    cur.execute("SELECT 1")
                    cur.fetchone()
                self.connection_pool.putconn(test_conn)
                
                logger.info("New pool initialized and tested successfully")
                self._connection_error_count = 0
                return
                
            except Exception as e:
                logger.error(f"Attempt {attempt + 1}/{max_attempts} to reinitialize pool failed: {e}")
                if attempt < max_attempts - 1:
                    time.sleep(5)  # Ждем больше перед повторной попыткой
                else:
                    logger.critical("FAILED TO REINITIALIZE POOL AFTER ALL ATTEMPTS")
                    raise Exception("Database pool recovery failed completely")
    
    def emergency_recovery(self):
        """Аварийное восстановление при полном отказе пула"""
        logger.critical("EMERGENCY RECOVERY: Attempting full database reconnection")
        
        # Сбрасываем счетчики
        self._connection_error_count = 0
        self._last_error_reset = time.time()
        
        # Закрываем все что можно закрыть
        if self.connection_pool:
            try:
                # Агрессивное закрытие всех соединений
                if hasattr(self.connection_pool, '_pool'):
                    for conn in list(self.connection_pool._pool):
                        try:
                            conn.close()
                        except:
                            pass
                    self.connection_pool._pool.clear()
                
                self.connection_pool.close()
            except:
                pass
            finally:
                self.connection_pool = None
        
        # Большая пауза для полного сброса
        logger.info("Waiting 5 seconds for complete reset...")
        time.sleep(5)
        
        # Пытаемся создать новый пул
        try:
            self._initialize_pool()
            logger.info("Emergency recovery successful")
            return True
        except Exception as e:
            logger.critical(f"Emergency recovery failed: {e}")
            return False
    
    def close(self):
        """Закрытие пула соединений"""
        if self.connection_pool:
            try:
                self.connection_pool.close()
                logger.info("Пул подключений закрыт")
            except Exception as e:
                logger.error(f"Ошибка при закрытии пула: {e}")

    def __del__(self):
        """Деструктор для автоматического закрытия пула"""
        self.close()

    @contextmanager
    def transaction(self):
        """
        Контекстный менеджер для выполнения транзакций
        
        Использование:
            with db.transaction() as conn:
                with conn.cursor(row_factory=dict_row) as cur:
                    cur.execute("INSERT...")
                    cur.execute("UPDATE...")
                # Автоматический commit при успехе или rollback при ошибке
        """
        connection = None
        try:
            with self.get_connection() as conn:
                connection = conn
                yield conn
                # Если не было исключений, коммитим
                if conn and not conn.closed:
                    conn.commit()
        except Exception as e:
            # При ошибке откатываем транзакцию
            if connection and not connection.closed:
                try:
                    connection.rollback()
                except Exception as rollback_error:
                    logger.error(f"Failed to rollback transaction: {rollback_error}")
            raise
    
    def execute_query(self, query, params=None, fetch=False, retry_on_error=True):
        """
        Выполнение SQL запроса с автоматическим восстановлением при EOF
        
        Args:
            query (str): SQL запрос
            params (tuple): Параметры для запроса
            fetch (bool): Нужно ли возвращать результат
            retry_on_error (bool): Повторять ли при ошибке соединения
            
        Returns:
            list: Результат запроса, если fetch=True
        """
        max_retries = Config.DB_MAX_RETRIES if retry_on_error else 1
        last_error = None
        
        for attempt in range(max_retries):
            try:
                with self.get_connection() as conn:
                    # Устанавливаем таймаут для операций
                    conn.execute("SET statement_timeout = '30s'")
                    with conn.cursor(row_factory=dict_row) as cur:
                        cur.execute(query, params)
                        
                        # КРИТИЧНО: Проверяем что запрос действительно возвращает результат
                        if fetch:
                            # Проверяем, есть ли результат для fetch
                            if cur.description is not None:
                                result = cur.fetchall()
                            else:
                                # Запрос не вернул результат (UPDATE/INSERT/DELETE)
                                result = []
                                logger.warning(f"Fetch requested but query produced no result: {query[:100]}")
                            conn.commit()
                            return result
                        else:
                            # Для не-SELECT запросов возвращаем количество затронутых строк
                            affected_rows = cur.rowcount if cur.rowcount > 0 else 0
                            conn.commit()
                            return affected_rows
                            
            except (psycopg.OperationalError, psycopg.errors.DuplicatePreparedStatement, psycopg.ProgrammingError, OSError) as e:
                last_error = e
                error_msg = str(e).lower()
                
                # Критическая ошибка - invalid socket означает полный разрыв соединения
                if 'invalid socket' in error_msg or 'bad file descriptor' in error_msg:
                    logger.error(f"Critical socket error detected: {e}")
                    # Пересоздаем пул соединений при критических ошибках сокета
                    if self.error_count >= Config.DB_MAX_ERRORS_BEFORE_REINIT:
                        logger.warning("Reinitializing connection pool due to socket errors")
                        self._reinit_pool()
                    if attempt < max_retries - 1:
                        wait_time = (attempt + 1) * 2  # Прогрессивная задержка
                        logger.info(f"Waiting {wait_time}s before retry after socket error...")
                        time.sleep(wait_time)
                        continue
                
                # Обработка различных типов ошибок
                if 'prepared statement' in error_msg and attempt < max_retries - 1:
                    logger.warning(f"Duplicate prepared statement on attempt {attempt + 1}/{max_retries}: {e}")
                    time.sleep(0.5)  # Увеличиваем паузу
                    continue
                
                # Ошибка "last operation didn't produce a result" - часто из-за неправильного fetch
                if "didn't produce a result" in error_msg:
                    logger.error(f"Fetch error on non-SELECT query: {query[:100]}")
                    # Не повторяем, возвращаем пустой результат если это был fetch
                    if fetch:
                        return []
                    return None
                
                # Если это ошибка EOF или разрыва соединения
                if any(x in error_msg for x in [
                    'eof detected', 'connection reset', 'broken pipe', 
                    'consuming input failed', 'server closed the connection',
                    'connection unexpectedly closed', 'terminating connection'
                ]):
                    logger.warning(f"Connection error on attempt {attempt + 1}/{max_retries}: {e}")
                    
                    # Увеличиваем счетчик ошибок
                    self._track_connection_error()
                    
                    if attempt < max_retries - 1:
                        # Прогрессивная задержка: 1s, 2s, 4s
                        delay = min(2 ** attempt, 5)  # Максимум 5 секунд
                        logger.info(f"Waiting {delay}s before retry...")
                        time.sleep(delay)
                        
                        if self.use_pool:
                            # Проверяем, нужно ли переинициализировать пул
                            if self._connection_error_count >= self._max_errors_before_reinit:
                                # Слишком много ошибок - пересоздаем пул
                                logger.warning(f"Too many connection errors ({self._connection_error_count}), reinitializing pool")
                                try:
                                    self.reinitialize_pool()
                                    self._connection_error_count = 0  # Сбрасываем счетчик
                                except Exception as reinit_error:
                                    logger.error(f"Failed to reinitialize pool: {reinit_error}")
                            else:
                                # Просто проверяем здоровье пула
                                self.check_pool_health()
                        continue
                
                # Если это другая ошибка или исчерпали попытки
                raise
                
            except Exception as e:
                # Для других ошибок не повторяем
                logger.error(f"Query execution error: {e}")
                raise
        
        # Если все попытки исчерпаны
        if last_error:
            raise last_error

    def initialize_schema(self):
        """Проверка существования необходимых таблиц в базе данных"""
        try:
            # Проверяем существование схемы web
            check_schema = """
                SELECT EXISTS (
                    SELECT 1 FROM information_schema.schemata 
                    WHERE schema_name = 'web'
                );
            """
            result = self.execute_query(check_schema, fetch=True)
            
            if result and result[0]['exists']:
                logger.info("Схема web существует, таблицы уже инициализированы")
            else:
                logger.warning("Схема web не найдена. Создание базовых таблиц пропущено.")
                logger.info("Используйте существующую структуру базы данных")
                
        except Exception as e:
            logger.warning(f"Не удалось проверить схему базы данных: {e}")
            logger.info("Продолжаем работу с существующей структурой БД")


# Функции для работы с пользователями
def create_user(db, username, password_hash, is_admin=False, is_approved=False):
    """Создание нового пользователя"""
    query = """
        INSERT INTO users (username, password_hash, is_admin, is_approved)
        VALUES (%s, %s, %s, %s)
        RETURNING id
    """
    result = db.execute_query(query, (username, password_hash, is_admin, is_approved), fetch=True)
    return result[0]['id'] if result else None


def get_user_by_username(db, username):
    """Получение пользователя по имени"""
    query = "SELECT * FROM users WHERE username = %s"
    result = db.execute_query(query, (username,), fetch=True)
    return result[0] if result else None


def get_user_by_id(db, user_id):
    """Получение пользователя по ID"""
    query = "SELECT * FROM users WHERE id = %s"
    result = db.execute_query(query, (user_id,), fetch=True)
    return result[0] if result else None


def is_first_user(db):
    """Проверка, является ли это первым пользователем в системе"""
    query = "SELECT COUNT(*) as count FROM users"
    result = db.execute_query(query, fetch=True)
    return result[0]['count'] == 0


def get_unapproved_users(db):
    """Получение списка неподтвержденных пользователей"""
    query = """
        SELECT id, username, created_at 
        FROM users 
        WHERE is_approved = FALSE AND is_admin = FALSE
        ORDER BY created_at DESC
    """
    return db.execute_query(query, fetch=True)


def approve_user(db, user_id):
    """Подтверждение пользователя администратором"""
    query = "UPDATE users SET is_approved = TRUE WHERE id = %s"
    db.execute_query(query, (user_id,))


# Функции для работы с данными о сделках (существующая таблица large_trades)
def get_trading_data(db, time_filter=None, min_value_usd=None, operation_type=None):
    """
    Получение агрегированных данных о торгах для дашборда
    
    Args:
        db: объект базы данных
        time_filter (str): временной фильтр ('1h', '4h', '12h', '24h', '7d')
        min_value_usd (float): минимальная сумма сделки в USD
        operation_type (str): тип операций ('buys', 'sells', 'both')
    
    Returns:
        list: агрегированные данные по активам
    """
    # Базовый запрос для агрегации данных по активам
    query = """
        SELECT 
            base_asset,
            SUM(CASE WHEN is_sell = FALSE THEN value_usd ELSE 0 END) as total_buys,
            SUM(CASE WHEN is_sell = TRUE THEN value_usd ELSE 0 END) as total_sells,
            SUM(CASE WHEN is_sell = FALSE THEN value_usd ELSE 0 END) - 
            SUM(CASE WHEN is_sell = TRUE THEN value_usd ELSE 0 END) as net_flow,
            COUNT(*) as total_trades
        FROM large_trades
        WHERE 1=1
    """

    params = []

    # Исключаем стейблкоины
    query += """
        AND base_asset NOT ILIKE '%%USD%%'
        AND base_asset NOT ILIKE '%%USDT%%'
        AND base_asset NOT ILIKE '%%USDC%%'
        AND base_asset NOT ILIKE '%%BUSD%%'
        AND base_asset NOT ILIKE '%%DAI%%'
        AND base_asset NOT ILIKE '%%TUSD%%'
        AND base_asset NOT ILIKE '%%USDE%%'
        AND base_asset NOT ILIKE '%%USD1%%'
        AND base_asset NOT ILIKE '%%FDUSD%%'
        AND base_asset NOT ILIKE '%%PYUSD%%'
    """

    # Добавление фильтра по типу операций
    if operation_type and operation_type != 'both':
        if operation_type == 'buys':
            query += " AND is_sell = FALSE"
        elif operation_type == 'sells':
            query += " AND is_sell = TRUE"

    # Добавление временного фильтра
    if time_filter:
        time_conditions = {
            '1h': "created_at >= NOW() - INTERVAL '1 hour'",
            '4h': "created_at >= NOW() - INTERVAL '4 hours'",
            '12h': "created_at >= NOW() - INTERVAL '12 hours'",
            '24h': "created_at >= NOW() - INTERVAL '24 hours'",
            '7d': "created_at >= NOW() - INTERVAL '7 days'"
        }
        if time_filter in time_conditions:
            query += f" AND {time_conditions[time_filter]}"

    # Добавление фильтра по минимальной сумме (учитываем constraint >= 10000)
    if min_value_usd and min_value_usd > 10000:
        query += " AND value_usd >= %s"
        params.append(min_value_usd)

    query += """
        GROUP BY base_asset
        HAVING SUM(value_usd) > 0
        ORDER BY net_flow DESC
        LIMIT 50
    """

    return db.execute_query(query, tuple(params), fetch=True)


def get_trading_stats(db, time_filter=None, min_value_usd=None, operation_type=None):
    """Получение общей статистики торгов"""
    query = """
        SELECT 
            COUNT(*) as total_trades,
            SUM(value_usd) as total_volume,
            COUNT(DISTINCT base_asset) as total_assets,
            AVG(value_usd) as avg_trade_size,
            MAX(value_usd) as max_trade_size
        FROM large_trades
        WHERE 1=1
    """

    params = []

    # Исключаем стейблкоины
    query += """
        AND base_asset NOT ILIKE '%%USD%%'
        AND base_asset NOT ILIKE '%%USDT%%'
        AND base_asset NOT ILIKE '%%USDC%%'
        AND base_asset NOT ILIKE '%%BUSD%%'
        AND base_asset NOT ILIKE '%%DAI%%'
        AND base_asset NOT ILIKE '%%TUSD%%'
        AND base_asset NOT ILIKE '%%USDE%%'
        AND base_asset NOT ILIKE '%%USD1%%'
        AND base_asset NOT ILIKE '%%FDUSD%%'
        AND base_asset NOT ILIKE '%%PYUSD%%'
    """

    # Добавление фильтра по типу операций
    if operation_type and operation_type != 'both':
        if operation_type == 'buys':
            query += " AND is_sell = FALSE"
        elif operation_type == 'sells':
            query += " AND is_sell = TRUE"

    if time_filter:
        time_conditions = {
            '1h': "created_at >= NOW() - INTERVAL '1 hour'",
            '4h': "created_at >= NOW() - INTERVAL '4 hours'",
            '12h': "created_at >= NOW() - INTERVAL '12 hours'",
            '24h': "created_at >= NOW() - INTERVAL '24 hours'",
            '7d': "created_at >= NOW() - INTERVAL '7 days'"
        }
        if time_filter in time_conditions:
            query += f" AND {time_conditions[time_filter]}"

    if min_value_usd and min_value_usd > 10000:
        query += " AND value_usd >= %s"
        params.append(min_value_usd)

    result = db.execute_query(query, tuple(params), fetch=True)
    return result[0] if result else None


def initialize_signals_with_params(db, hours_back=24, tp_percent=4.0, sl_percent=3.0):
    """
    Полная инициализация системы с использованием fas_v2.market_data_aggregated
    """
    try:
        print(f"[INIT] ========== НАЧАЛО ИНИЦИАЛИЗАЦИИ ==========")
        print(f"[INIT] Параметры: hours={hours_back}, TP={tp_percent}%, SL={sl_percent}%")

        # Очистка таблицы
        print("[INIT] Очистка таблицы web_signals...")
        db.execute_query("TRUNCATE TABLE web.web_signals")

        # Получаем сигналы
        signals_query = """
            SELECT 
                pr.signal_id,
                sh.pair_symbol,
                sh.trading_pair_id,
                pr.signal_type as signal_action,
                pr.created_at as signal_timestamp,
                tp.exchange_id,
                ex.exchange_name
            FROM smart_ml.predictions pr
            JOIN fas_v2.scoring_history sh ON sh.id = pr.signal_id
            JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
            JOIN public.exchanges ex ON ex.id = tp.exchange_id
            WHERE pr.created_at >= NOW() - (INTERVAL '1 hour' * %s)
                AND pr.prediction = true
                AND tp.contract_type_id = 1
                AND tp.exchange_id IN (1, 2)
            ORDER BY pr.created_at DESC
        """

        signals = db.execute_query(signals_query, (hours_back,), fetch=True)
        print(f"[INIT] Найдено {len(signals) if signals else 0} сигналов")

        if not signals:
            return {'initialized': 0, 'closed_tp': 0, 'closed_sl': 0, 'open': 0, 'errors': 0}

        stats = {
            'initialized': 0,
            'closed_tp': 0,
            'closed_sl': 0,
            'open': 0,
            'errors': 0,
            'total_max_profit': 0,
            'total_realized_profit': 0,
            'missed_profit': 0,
            'by_exchange': {'Binance': 0, 'Bybit': 0}
        }

        for idx, signal in enumerate(signals):
            try:
                # Добавляем exchange_name в сигнал
                signal_with_exchange = dict(signal)
                signal_with_exchange['signal_timestamp'] = make_aware(signal_with_exchange['signal_timestamp'])

                result = process_signal_complete(
                    db,
                    signal_with_exchange,
                    tp_percent=tp_percent,
                    sl_percent=sl_percent
                )

                if result.get('success'):
                    stats['initialized'] += 1
                    stats['by_exchange'][signal['exchange_name']] += 1

                    if result.get('is_closed'):
                        if result['close_reason'] == 'take_profit':
                            stats['closed_tp'] += 1
                            stats['total_realized_profit'] += result.get('realized_pnl', 0)
                            if result.get('max_profit', 0) > result.get('realized_pnl', 0):
                                stats['missed_profit'] += (result['max_profit'] - result['realized_pnl'])
                        elif result['close_reason'] == 'stop_loss':
                            stats['closed_sl'] += 1
                    else:
                        stats['open'] += 1

                    stats['total_max_profit'] += result.get('max_profit', 0)
                elif result.get('reason') == 'duplicate_position':
                    # Учитываем пропущенные дубликаты
                    if 'duplicates_skipped' not in stats:
                        stats['duplicates_skipped'] = 0
                    stats['duplicates_skipped'] += 1
                    print(f"[INIT] Пропущен дубликат для {signal['pair_symbol']} - уже есть открытая позиция")
                else:
                    stats['errors'] += 1

                if idx % 20 == 0:
                    print(f"[INIT] Обработано {idx}/{len(signals)} сигналов...")

            except Exception as e:
                print(f"[INIT] Ошибка при обработке сигнала {signal['signal_id']}: {e}")
                stats['errors'] += 1
                continue

        # Расчет эффективности
        if stats['total_max_profit'] > 0:
            efficiency = (stats['total_realized_profit'] / stats['total_max_profit']) * 100
        else:
            efficiency = 0

        print(f"[INIT] ========== РЕЗУЛЬТАТЫ ==========")
        print(f"[INIT] Инициализировано: {stats['initialized']}")
        print(f"[INIT]   - Binance: {stats['by_exchange']['Binance']}")
        print(f"[INIT]   - Bybit: {stats['by_exchange']['Bybit']}")
        print(f"[INIT] Пропущено дубликатов: {stats.get('duplicates_skipped', 0)}")
        print(f"[INIT] Закрыто по TP: {stats['closed_tp']}")
        print(f"[INIT] Закрыто по SL: {stats['closed_sl']}")
        print(f"[INIT] Остались открытыми: {stats['open']}")
        print(f"[INIT] Ошибок: {stats['errors']}")
        print(f"[INIT] ========== АНАЛИЗ ПРОФИТА ==========")
        print(f"[INIT] Максимально возможный профит: ${stats['total_max_profit']:.2f}")
        print(f"[INIT] Реализованный профит: ${stats['total_realized_profit']:.2f}")
        print(f"[INIT] Упущенный профит: ${stats['missed_profit']:.2f}")
        print(f"[INIT] Эффективность TP: {efficiency:.1f}%")

        return stats

    except Exception as e:
        print(f"[INIT] КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return {'initialized': 0, 'closed_tp': 0, 'closed_sl': 0, 'open': 0, 'errors': 1}


def has_open_position(db, pair_symbol):
    """
    Проверяет наличие открытых позиций по символу пары
    
    Args:
        db: объект базы данных
        pair_symbol: символ торговой пары
    
    Returns:
        dict с информацией об открытой позиции или None
    """
    try:
        query = """
            SELECT 
                signal_id,
                pair_symbol,
                entry_price,
                signal_timestamp,
                position_size_usd,
                leverage,
                use_trailing_stop,
                score_week,
                score_month
            FROM web.web_signals
            WHERE pair_symbol = %s 
                AND is_closed = FALSE
            ORDER BY signal_timestamp DESC
            LIMIT 1
        """
        
        result = db.execute_query(query, (pair_symbol,), fetch=True)
        
        if result and len(result) > 0:
            logger.info(f"[DUPLICATE CHECK] Найдена открытая позиция по {pair_symbol}: signal_id={result[0]['signal_id']}")
            return result[0]
        
        return None
        
    except Exception as e:
        logger.error(f"[DUPLICATE CHECK] Ошибка проверки открытых позиций для {pair_symbol}: {e}")
        return None


def process_signal_complete(db, signal,
                            tp_percent=None, sl_percent=None,
                            position_size=None, leverage=None,
                            use_trailing_stop=None,
                            trailing_distance_pct=None,
                            trailing_activation_pct=None,
                            exchange_id=None):
    """
    Обработка сигнала с поддержкой Trailing Stop
    Использует дефолтные значения из Config если параметры не переданы

    Args:
        exchange_id: ID биржи из public.exchanges (опционально, берется из signal)
    """
    # Использовать значения из Config если не переданы
    tp_percent = tp_percent if tp_percent is not None else Config.DEFAULT_TAKE_PROFIT_PERCENT
    sl_percent = sl_percent if sl_percent is not None else Config.DEFAULT_STOP_LOSS_PERCENT
    position_size = position_size if position_size is not None else Config.DEFAULT_POSITION_SIZE
    leverage = leverage if leverage is not None else Config.DEFAULT_LEVERAGE
    use_trailing_stop = use_trailing_stop if use_trailing_stop is not None else Config.DEFAULT_USE_TRAILING_STOP
    trailing_distance_pct = trailing_distance_pct if trailing_distance_pct is not None else Config.DEFAULT_TRAILING_DISTANCE_PCT
    trailing_activation_pct = trailing_activation_pct if trailing_activation_pct is not None else Config.DEFAULT_TRAILING_ACTIVATION_PCT
    try:
        signal_id = signal['signal_id']
        trading_pair_id = signal['trading_pair_id']
        pair_symbol = signal['pair_symbol']
        score_week = signal.get('score_week', 0)
        score_month = signal.get('score_month', 0)
        signal_action = signal['signal_action']
        signal_timestamp = signal['signal_timestamp']
        exchange_name = signal.get('exchange_name', 'Unknown')

        # Получаем exchange_id из signal если не передан явно
        if exchange_id is None:
            exchange_id = signal.get('exchange_id')
        
        # Проверяем наличие открытых позиций по этой паре
        open_position = has_open_position(db, pair_symbol)
        if open_position:
            logger.warning(f"[DUPLICATE SKIPPED] Пропускаем сигнал {signal_id} для {pair_symbol} - уже есть открытая позиция (signal_id={open_position['signal_id']})")
            return {'success': False, 'reason': 'duplicate_position', 'existing_signal_id': open_position['signal_id']}

        # Инициализируем last_price значением по умолчанию
        last_price = None

        # Получаем цену входа (используем helper function для миграции на public.candles)
        entry_price_query = build_entry_price_query(window_minutes=5)
        ts_param = convert_timestamp_param(signal_timestamp)

        price_result = db.execute_query(
            entry_price_query,
            (trading_pair_id, ts_param, ts_param, ts_param),
            fetch=True
        )

        if not price_result:
            # Расширенный поиск (используем helper function)
            fallback_query = build_entry_price_fallback_query(window_hours=1)
            price_result = db.execute_query(
                fallback_query,
                (trading_pair_id, ts_param, ts_param, ts_param),
                fetch=True
            )

        if not price_result:
            print(f"[PROCESS] Нет цены для {pair_symbol} ({exchange_name})")
            return {'success': False}

        entry_price = float(price_result[0]['entry_price'])

        # Устанавливаем last_price = entry_price по умолчанию
        last_price = entry_price

        # Получаем историю (24 часа от signal_timestamp для полной симуляции)
        # Используем helper function для миграции на public.candles
        history_query = build_candle_history_query(duration_hours=24)
        start_ts = convert_timestamp_param(signal_timestamp)

        # Для public.candles: end_ts = start_ts + 24h в миллисекундах
        from datetime import timedelta
        if Config.USE_PUBLIC_CANDLES:
            end_ts = start_ts + (24 * 60 * 60 * 1000)  # +24 часа в миллисекундах
        else:
            end_ts = signal_timestamp

        history = db.execute_query(history_query, (trading_pair_id, start_ts, end_ts), fetch=True)

        if not history:
            # Сохраняем с начальными данными (с обработкой дубликатов)
            insert_query = """
                INSERT INTO web.web_signals (
                    signal_id, pair_symbol, signal_action, signal_timestamp,
                    entry_price, position_size_usd, leverage,
                    trailing_stop_percent, take_profit_percent,
                    is_closed, last_known_price, use_trailing_stop,
                    score_week, score_month, exchange_id
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s, %s
                )
                ON CONFLICT (signal_id) DO UPDATE SET
                    last_updated_at = NOW()
            """
            db.execute_query(insert_query, (
                signal_id, pair_symbol, signal_action, signal_timestamp,
                entry_price, position_size, leverage,
                trailing_distance_pct if use_trailing_stop else sl_percent,
                tp_percent, entry_price, use_trailing_stop,
                score_week, score_month, exchange_id
            ))
            return {'success': True, 'is_closed': False, 'close_reason': None, 'max_profit': 0}

        # Обновляем last_price из истории если есть данные
        if history:
            last_price = float(history[-1]['close_price'])

        # ВЫБОР ЛОГИКИ: Trailing Stop или Fixed TP/SL
        if use_trailing_stop:
            # Вычисляем simulation_end_time (24 часа от signal_timestamp)
            from datetime import timedelta
            simulation_end_time = signal_timestamp + timedelta(hours=24) if signal_timestamp else None

            # Используем новую функцию trailing stop
            result = calculate_trailing_stop_exit(
                entry_price, history, signal_action,
                trailing_distance_pct, trailing_activation_pct,
                sl_percent, position_size, leverage,
                signal_timestamp,  # Передаем timestamp для корректного расчета таймаута
                Config.DEFAULT_COMMISSION_RATE,  # Передаем commission_rate
                simulation_end_time  # КРИТИЧНО: передаем simulation_end_time!
            )

            is_closed = result['is_closed']
            close_price = result['close_price']
            close_time = result['close_time']
            close_reason = result['close_reason']
            realized_pnl = result['pnl_usd'] if is_closed else 0
            max_profit = result['max_profit_usd']
            trailing_activated = result.get('trailing_activated', False)

            # Для открытых позиций
            if not is_closed:
                if signal_action in ['SELL', 'SHORT']:
                    unrealized_percent = ((entry_price - last_price) / entry_price) * 100
                else:
                    unrealized_percent = ((last_price - entry_price) / entry_price) * 100
                unrealized_pnl = position_size * (unrealized_percent / 100) * leverage
            else:
                unrealized_pnl = 0

        else:
            # СУЩЕСТВУЮЩАЯ ЛОГИКА Fixed TP/SL с учетом комиссий
            # Расчет комиссий
            commission_rate = Config.DEFAULT_COMMISSION_RATE
            effective_position = position_size * leverage
            entry_commission = effective_position * commission_rate
            exit_commission = effective_position * commission_rate
            total_commission = entry_commission + exit_commission

            max_profit = 0
            max_profit_price = entry_price
            is_closed = False
            close_price = None
            close_time = None
            close_reason = None
            realized_pnl = 0
            best_price_ever = entry_price
            trailing_activated = False

            # Рассчитываем уровни TP и SL
            if signal_action in ['SELL', 'SHORT']:
                tp_price = entry_price * (1 - tp_percent / 100)
                sl_price = entry_price * (1 + sl_percent / 100)
            else:  # BUY, LONG
                tp_price = entry_price * (1 + tp_percent / 100)
                sl_price = entry_price * (1 - sl_percent / 100)

            # Проходим по истории (существующая логика)
            for candle in history:
                current_time = candle['timestamp']
                high_price = float(candle['high_price'])
                low_price = float(candle['low_price'])

                # Обновляем лучшую цену и максимальный профит
                if signal_action in ['SELL', 'SHORT']:
                    if low_price < best_price_ever:
                        best_price_ever = low_price
                        max_profit_percent = ((entry_price - best_price_ever) / entry_price) * 100
                        gross_potential_max_profit = effective_position * (max_profit_percent / 100)
                        potential_max_profit = gross_potential_max_profit - total_commission  # Вычитаем комиссии
                        if potential_max_profit > max_profit:
                            max_profit = potential_max_profit
                            max_profit_price = best_price_ever

                    # Проверка закрытия для SHORT
                    if not is_closed:
                        # Проверка ликвидации (приоритет!)
                        liquidation_threshold = Config.LIQUIDATION_THRESHOLD
                        liquidation_loss_pct = -(100 / leverage) * liquidation_threshold
                        unrealized_pnl_pct = ((entry_price - high_price) / entry_price) * 100
                        if unrealized_pnl_pct <= liquidation_loss_pct:
                            is_closed = True
                            close_reason = 'liquidation'
                            # КРИТИЧНО: Ликвидация по liquidation_price, не по actual price
                            close_price = entry_price * (1 - liquidation_loss_pct / 100)
                            close_time = current_time
                        elif low_price <= tp_price:
                            is_closed = True
                            close_reason = 'take_profit'
                            close_price = tp_price
                            close_time = current_time
                        elif high_price >= sl_price:
                            is_closed = True
                            close_reason = 'stop_loss'
                            close_price = sl_price
                            close_time = current_time
                else:  # BUY, LONG
                    if high_price > best_price_ever:
                        best_price_ever = high_price
                        max_profit_percent = ((best_price_ever - entry_price) / entry_price) * 100
                        gross_potential_max_profit = effective_position * (max_profit_percent / 100)
                        potential_max_profit = gross_potential_max_profit - total_commission  # Вычитаем комиссии
                        if potential_max_profit > max_profit:
                            max_profit = potential_max_profit
                            max_profit_price = best_price_ever

                    # Проверка закрытия для LONG
                    if not is_closed:
                        # Проверка ликвидации (приоритет!)
                        liquidation_threshold = Config.LIQUIDATION_THRESHOLD
                        liquidation_loss_pct = -(100 / leverage) * liquidation_threshold
                        unrealized_pnl_pct = ((low_price - entry_price) / entry_price) * 100
                        if unrealized_pnl_pct <= liquidation_loss_pct:
                            is_closed = True
                            close_reason = 'liquidation'
                            # КРИТИЧНО: Ликвидация по liquidation_price, не по actual price
                            close_price = entry_price * (1 + liquidation_loss_pct / 100)
                            close_time = current_time
                        elif high_price >= tp_price:
                            is_closed = True
                            close_reason = 'take_profit'
                            close_price = tp_price
                            close_time = current_time
                        elif low_price <= sl_price:
                            is_closed = True
                            close_reason = 'stop_loss'
                            close_price = sl_price
                            close_time = current_time

            # Если не закрылась, проверяем таймаут (24 часа)
            if not is_closed:
                hours_passed = (history[-1]['timestamp'] - signal_timestamp).total_seconds() / 3600
                if hours_passed >= 24:
                    is_closed = True
                    close_reason = 'timeout'
                    close_price = last_price
                    close_time = history[-1]['timestamp']

            # Рассчитываем realized PnL если закрыта (с учетом комиссий)
            if is_closed:
                if signal_action in ['SELL', 'SHORT']:
                    pnl_percent = ((entry_price - close_price) / entry_price) * 100
                else:
                    pnl_percent = ((close_price - entry_price) / entry_price) * 100
                gross_pnl = effective_position * (pnl_percent / 100)
                realized_pnl = gross_pnl - total_commission  # NET PnL после комиссий

                # Применяем ограничение isolated margin
                # КРИТИЧНО: При isolated margin максимальный убыток = начальная маржа + все комиссии
                max_loss = -(position_size + entry_commission + exit_commission)
                if realized_pnl < max_loss:
                    print(f"[ISOLATED MARGIN CAP] Capping loss from ${realized_pnl:.2f} to ${max_loss:.2f} (position: ${position_size}, entry_fee: ${entry_commission:.2f}, exit_fee: ${exit_commission:.2f})")
                    realized_pnl = max_loss

            # Рассчитываем unrealized PnL если открыта
            unrealized_pnl = 0
            if not is_closed:
                if signal_action in ['SELL', 'SHORT']:
                    unrealized_percent = ((entry_price - last_price) / entry_price) * 100
                else:
                    unrealized_percent = ((last_price - entry_price) / entry_price) * 100
                unrealized_pnl = position_size * (unrealized_percent / 100) * leverage

        # Отладочный вывод для закрытых по TP/trailing позиций
        if close_reason in ['take_profit', 'trailing_stop'] and max_profit > realized_pnl * 1.1:
            print(f"[PROCESS] {pair_symbol} ({exchange_name}): {close_reason} at {close_price:.8f} "
                  f"(profit: ${realized_pnl:.2f}), but max was ${max_profit:.2f}")

        # Сохраняем в БД с обработкой дубликатов
        insert_query = """
            INSERT INTO web.web_signals (
                signal_id, pair_symbol, signal_action, signal_timestamp,
                entry_price, position_size_usd, leverage,
                trailing_stop_percent, take_profit_percent,
                is_closed, closing_price, closed_at, close_reason,
                realized_pnl_usd, unrealized_pnl_usd,
                max_potential_profit_usd, last_known_price,
                use_trailing_stop, trailing_activated,
                score_week, score_month, exchange_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            ON CONFLICT (signal_id) DO UPDATE SET
                pair_symbol = EXCLUDED.pair_symbol,
                signal_action = EXCLUDED.signal_action,
                entry_price = EXCLUDED.entry_price,
                position_size_usd = EXCLUDED.position_size_usd,
                leverage = EXCLUDED.leverage,
                trailing_stop_percent = EXCLUDED.trailing_stop_percent,
                take_profit_percent = EXCLUDED.take_profit_percent,
                is_closed = EXCLUDED.is_closed,
                closing_price = EXCLUDED.closing_price,
                closed_at = EXCLUDED.closed_at,
                close_reason = EXCLUDED.close_reason,
                realized_pnl_usd = EXCLUDED.realized_pnl_usd,
                unrealized_pnl_usd = EXCLUDED.unrealized_pnl_usd,
                max_potential_profit_usd = EXCLUDED.max_potential_profit_usd,
                last_known_price = EXCLUDED.last_known_price,
                use_trailing_stop = EXCLUDED.use_trailing_stop,
                trailing_activated = EXCLUDED.trailing_activated,
                score_week = EXCLUDED.score_week,
                score_month = EXCLUDED.score_month,
                exchange_id = EXCLUDED.exchange_id,
                last_updated_at = NOW()
        """

        # Определяем был ли активирован trailing (только для trailing stop режима)
        if not use_trailing_stop:
            trailing_activated = False

        db.execute_query(insert_query, (
            signal_id, pair_symbol, signal_action, signal_timestamp,
            entry_price, position_size, leverage,
            trailing_distance_pct if use_trailing_stop else sl_percent,
            trailing_activation_pct if use_trailing_stop else tp_percent,
            is_closed, close_price, close_time, close_reason,
            realized_pnl if is_closed else 0,
            unrealized_pnl if not is_closed else 0,
            max_profit, last_price, use_trailing_stop, trailing_activated,
            score_week, score_month, exchange_id
        ))

        return {
            'success': True,
            'is_closed': is_closed,
            'close_reason': close_reason,
            'max_profit': max_profit,
            'realized_pnl': realized_pnl if is_closed else 0,
            'efficiency': (realized_pnl / max_profit * 100) if is_closed and max_profit > 0 else None
        }

    except Exception as e:
        print(f"[PROCESS] Ошибка для сигнала {signal['signal_id']}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False}


def calculate_trailing_stop_exit(entry_price, history, signal_action,
                                 trailing_distance_pct, trailing_activation_pct,
                                 sl_percent, position_size, leverage,
                                 signal_timestamp=None, commission_rate=None,
                                 simulation_end_time=None):
    """
    Расчет выхода по 3-фазной торговой системе:
    - Фаза 1 (0-24ч): Активная торговля с TS/SL
    - Фаза 2 (24-32ч): Breakeven Window - закрытие в безубыток
    - Фаза 3 (32ч+): Smart Loss - 0.5% за каждый час

    ВАЖНО: signal_timestamp ОБЯЗАТЕЛЕН для определения фаз!

    Args:
        simulation_end_time: Время окончания симуляции (для принудительного закрытия по period_end)
    """
    from config import Config
    from datetime import timedelta
    import math

    # Расчет комиссий
    if commission_rate is None:
        commission_rate = Config.DEFAULT_COMMISSION_RATE

    effective_position = position_size * leverage
    entry_commission = effective_position * commission_rate
    exit_commission = effective_position * commission_rate
    total_commission = entry_commission + exit_commission

    # Параметры 3-фазной системы
    phase1_hours = Config.PHASE1_DURATION_HOURS  # 3
    phase2_hours = Config.PHASE2_DURATION_HOURS  # 8
    phase3_max_hours = Config.PHASE3_MAX_DURATION_HOURS  # 12
    smart_loss_rate = Config.SMART_LOSS_RATE_PER_HOUR  # 0.5

    # DEBUG: Логируем параметры в отдельный файл для отладки
    try:
        with open('/home/elcrypto/trading_assistant/phase_debug.log', 'a') as f:
            f.write(f"\n[DEBUG CALC_TS] ===== START =====\n")
            f.write(f"[DEBUG CALC_TS] signal_timestamp: {signal_timestamp}\n")
            f.write(f"[DEBUG CALC_TS] history length: {len(history) if history else 0}\n")
            f.write(f"[DEBUG CALC_TS] simulation_end_time: {simulation_end_time}\n")
            if history and len(history) > 0:
                f.write(f"[DEBUG CALC_TS] First candle: {history[0]['timestamp']}\n")
                f.write(f"[DEBUG CALC_TS] Last candle: {history[-1]['timestamp']}\n")
                if signal_timestamp:
                    duration = (history[-1]['timestamp'] - signal_timestamp).total_seconds() / 3600
                    f.write(f"[DEBUG CALC_TS] History duration: {duration:.2f} hours\n")
            f.write(f"[DEBUG CALC_TS] phase1_hours={phase1_hours}, phase2_hours={phase2_hours}, phase3_max_hours={phase3_max_hours}\n")
    except:
        pass

    # Временные границы фаз
    if signal_timestamp:
        phase1_end = signal_timestamp + timedelta(hours=phase1_hours)  # +3ч
        phase2_end = signal_timestamp + timedelta(hours=phase1_hours + phase2_hours)  # +11ч
        phase3_end = phase2_end + timedelta(hours=phase3_max_hours)  # +23ч (максимум)
        print(f"[DEBUG CALC_TS] Phase boundaries:")
        print(f"[DEBUG CALC_TS]   Phase 1: {signal_timestamp} → {phase1_end}")
        print(f"[DEBUG CALC_TS]   Phase 2: {phase1_end} → {phase2_end}")
        print(f"[DEBUG CALC_TS]   Phase 3: {phase2_end} → {phase3_end}")
    else:
        # Если timestamp не передан, используем старую логику (только для совместимости)
        phase1_end = None
        phase2_end = None
        phase3_end = None
        print(f"[DEBUG CALC_TS] WARNING: signal_timestamp is None!")

    # Переменные для отслеживания trailing stop
    is_trailing_active = False
    trailing_stop_price = None
    best_price_for_trailing = entry_price
    activation_candle_time = None

    # Переменная для отслеживания перехода в Фазу 2 из-за неактивации TS
    phase2_forced_entry = False
    phase2_forced_entry_time = None

    # ВАЖНО: Отдельная переменная для АБСОЛЮТНОГО максимума за весь период
    absolute_best_price = entry_price
    max_profit_usd = 0

    # Переменные для закрытия позиции
    is_closed = False
    close_price = None
    close_time = None
    close_reason = None

    # Расчет уровней
    is_long = signal_action in ['BUY', 'LONG']
    is_short = signal_action in ['SELL', 'SHORT']

    if is_short:
        activation_price = entry_price * (1 - trailing_activation_pct / 100)
        sl_price = entry_price * (1 + sl_percent / 100)
    else:  # LONG
        activation_price = entry_price * (1 + trailing_activation_pct / 100)
        sl_price = entry_price * (1 - sl_percent / 100)

    # Результат по умолчанию
    result = {
        'is_closed': False,
        'close_price': None,
        'close_time': None,
        'close_reason': None,
        'pnl_usd': 0,
        'pnl_percent': 0,
        'max_profit_usd': 0,
        'trailing_activated': False,
        'best_price': entry_price,
        'absolute_best_price': entry_price
    }

    if not history or len(history) == 0:
        return result

    # ГЛАВНЫЙ ЦИКЛ - обрабатываем ВСЮ историю
    for candle in history:
        candle_time = candle['timestamp']
        high_price = float(candle['high_price'])
        low_price = float(candle['low_price'])

        # ============ ВСЕГДА обновляем абсолютный максимум ============
        if is_short:
            if low_price < absolute_best_price:
                absolute_best_price = low_price
                max_profit_percent = ((entry_price - absolute_best_price) / entry_price) * 100
                gross_max_profit = effective_position * (max_profit_percent / 100)
                max_profit_usd = gross_max_profit - total_commission
        else:  # LONG
            if high_price > absolute_best_price:
                absolute_best_price = high_price
                max_profit_percent = ((absolute_best_price - entry_price) / entry_price) * 100
                gross_max_profit = effective_position * (max_profit_percent / 100)
                max_profit_usd = gross_max_profit - total_commission

        # ============ Управление позицией (только если еще открыта) ============
        if not is_closed:
            # ПРОВЕРКА ЛИКВИДАЦИИ (приоритет выше всех остальных!)
            liquidation_threshold = Config.LIQUIDATION_THRESHOLD
            liquidation_loss_pct = -(100 / leverage) * liquidation_threshold  # Например: -(100/10)*0.9 = -9%

            if is_long:
                unrealized_pnl_pct = ((low_price - entry_price) / entry_price) * 100
            else:  # SHORT
                unrealized_pnl_pct = ((entry_price - high_price) / entry_price) * 100

            if unrealized_pnl_pct <= liquidation_loss_pct:
                is_closed = True
                close_reason = 'liquidation'
                # КРИТИЧНО: При isolated margin ликвидация происходит по liquidation_price,
                # а не по actual price! Используем цену соответствующую liquidation threshold
                if is_long:
                    close_price = entry_price * (1 + liquidation_loss_pct / 100)
                else:  # SHORT
                    close_price = entry_price * (1 - liquidation_loss_pct / 100)
                close_time = candle_time
                print(f"[LIQUIDATION] Price would be {low_price if is_long else high_price:.8f}, "
                      f"but liquidation occurs at {close_price:.8f} ({liquidation_loss_pct:.2f}%)")
                continue

            # ПРОВЕРКА ПЕРЕХОДА В ФАЗУ 2: ДОЛЖНА БЫТЬ ДО проверки Phase 1!
            # Если прошло 3 часа и TS не активирован -> принудительный переход в Фазу 2
            if not phase2_forced_entry and not is_trailing_active and phase1_end and candle_time >= phase1_end:
                phase2_forced_entry = True
                phase2_forced_entry_time = candle_time
                print(f"[PHASE TRANSITION] TS не активирован через {phase1_hours}ч. Принудительный переход в Фазу 2 (Breakeven Window)")

            # ФАЗА 1: Активная торговля (0-3ч) - TS/SL
            # ВАЖНО: Если TS не активирован через 3 часа -> переход в Фазу 2
            if not phase2_forced_entry and (phase1_end is None or candle_time <= phase1_end):
                # Stop Loss
                if (is_long and low_price <= sl_price) or (is_short and high_price >= sl_price):
                    is_closed = True
                    close_reason = 'stop_loss'
                    # Применяем slippage на stop-loss
                    if is_long:
                        close_price = sl_price * (1 - SLIPPAGE_PERCENT / 100)
                    else:
                        close_price = sl_price * (1 + SLIPPAGE_PERCENT / 100)
                    print(f"[SLIPPAGE] SL: {sl_price:.4f} -> Executed: {close_price:.4f}")
                    close_time = candle_time
                    continue

                # Trailing Stop логика
                if is_short:
                    # Обновляем best_price
                    if low_price < best_price_for_trailing:
                        best_price_for_trailing = low_price

                    # Активация trailing stop
                    if not is_trailing_active and best_price_for_trailing <= activation_price:
                        is_trailing_active = True
                        result['trailing_activated'] = True
                        activation_candle_time = candle_time
                        trailing_stop_price = best_price_for_trailing * (1 + trailing_distance_pct / 100)

                    # Обновление trailing stop
                    if is_trailing_active:
                        new_stop = best_price_for_trailing * (1 + trailing_distance_pct / 100)
                        # Для SHORT: trailing stop может только ПОНИЖАТЬСЯ
                        # (защищает прибыль при падении цены)
                        if trailing_stop_price is None or new_stop < trailing_stop_price:
                            trailing_stop_price = new_stop
                            print(f"  [TRAILING SHORT] Stop lowered to {trailing_stop_price:.4f}")

                    # Срабатывание trailing stop
                    if is_trailing_active and candle_time != activation_candle_time and high_price >= trailing_stop_price:
                        is_closed = True
                        close_reason = 'trailing_stop'
                        close_price = trailing_stop_price
                        close_time = candle_time

                else:  # LONG
                    # Обновляем best_price
                    if high_price > best_price_for_trailing:
                        best_price_for_trailing = high_price

                    # Активация trailing stop
                    if not is_trailing_active and best_price_for_trailing >= activation_price:
                        is_trailing_active = True
                        result['trailing_activated'] = True
                        activation_candle_time = candle_time
                        trailing_stop_price = best_price_for_trailing * (1 - trailing_distance_pct / 100)

                    # Обновление trailing stop
                    if is_trailing_active:
                        new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
                        # Для LONG: trailing stop может только ПОВЫШАТЬСЯ
                        # (защищает прибыль при росте цены)
                        if trailing_stop_price is None or new_stop > trailing_stop_price:
                            trailing_stop_price = new_stop
                            print(f"  [TRAILING LONG] Stop raised to {trailing_stop_price:.4f}")

                    # Срабатывание trailing stop
                    if is_trailing_active and candle_time != activation_candle_time and low_price <= trailing_stop_price:
                        is_closed = True
                        close_reason = 'trailing_stop'
                        close_price = trailing_stop_price
                        close_time = candle_time

            # ФАЗА 2: Breakeven Window (3-11ч)
            # Может начаться РАНЬШЕ если TS не активирован через 3 часа
            # ВАЖНО: используем if вместо elif, чтобы проверить сразу после установки phase2_forced_entry
            # КРИТИЧНО: phase2_forced_entry должен работать только ДО phase2_end!
            if ((phase1_end < candle_time <= phase2_end) or (phase2_forced_entry and candle_time <= phase2_end)) and not is_closed:
                try:
                    with open('/home/elcrypto/trading_assistant/phase_debug.log', 'a') as f:
                        f.write(f"[DEBUG PHASE 2] Вошли в Phase 2: candle_time={candle_time}, phase2_forced_entry={phase2_forced_entry}, is_long={is_long}, entry_price={entry_price}, high_price={high_price}, low_price={low_price}\n")
                except:
                    pass

                # ВАЖНО: Stop Loss ПРОДОЛЖАЕТ работать в Фазе 2
                if (is_long and low_price <= sl_price) or (is_short and high_price >= sl_price):
                    is_closed = True
                    close_reason = 'stop_loss'
                    if is_long:
                        close_price = sl_price * (1 - SLIPPAGE_PERCENT / 100)
                    else:
                        close_price = sl_price * (1 + SLIPPAGE_PERCENT / 100)
                    close_time = candle_time
                    print(f"[PHASE 2] Закрытие по SL: {close_price:.8f}")
                    break

                # Закрытие в безубыток при касании entry_price
                breakeven_condition = (is_long and high_price >= entry_price) or (is_short and low_price <= entry_price)
                print(f"[DEBUG PHASE 2] Breakeven check: condition={breakeven_condition}, is_long={is_long}, high>entry={high_price >= entry_price if is_long else 'N/A'}, low<entry={low_price <= entry_price if not is_long else 'N/A'}")

                if breakeven_condition:
                    is_closed = True
                    close_reason = 'breakeven'
                    close_price = entry_price
                    close_time = candle_time
                    print(f"[PHASE 2] ✓✓✓ Закрытие в безубыток: {close_price:.8f}")
                    break

                # Сброс флага принудительного входа после первой проверки
                if phase2_forced_entry and candle_time > phase2_end:
                    phase2_forced_entry = False

            # ФАЗА 3: Smart Loss ИЛИ SL (11-23ч, максимум 12 часов)
            # ВАЖНО: SL имеет приоритет над Smart Loss
            if candle_time > phase2_end and not is_closed:
                hours_into_phase3 = (candle_time - phase2_end).total_seconds() / 3600
                try:
                    with open('/home/elcrypto/trading_assistant/phase_debug.log', 'a') as f:
                        f.write(f"[DEBUG PHASE 3] Вошли в Phase 3: candle_time={candle_time}, phase2_end={phase2_end}, is_long={is_long}\n")
                        f.write(f"[DEBUG PHASE 3] hours_into_phase3={hours_into_phase3:.2f}, phase3_max_hours={phase3_max_hours}\n")
                except:
                    pass

                # Проверяем максимальную длительность Фазы 3 (12 часов)
                if hours_into_phase3 > phase3_max_hours or candle_time >= phase3_end:
                    is_closed = True
                    close_reason = 'period_end'
                    close_price = float(candle['close_price'])
                    close_time = candle_time
                    print(f"[PHASE 3] Принудительное закрытие: достигнут лимит {phase3_max_hours}ч")
                    break

                # ПРИОРИТЕТ 1: Stop Loss (проверяем ПЕРВЫМ)
                if (is_long and low_price <= sl_price) or (is_short and high_price >= sl_price):
                    is_closed = True
                    close_reason = 'stop_loss'
                    if is_long:
                        close_price = sl_price * (1 - SLIPPAGE_PERCENT / 100)
                    else:
                        close_price = sl_price * (1 + SLIPPAGE_PERCENT / 100)
                    close_time = candle_time
                    print(f"[PHASE 3] Закрытие по SL: {close_price:.8f}")
                    break

                # ПРИОРИТЕТ 2: Smart Loss (если SL не сработал)
                hours_into_loss = hours_into_phase3
                loss_multiplier = max(1, math.ceil(hours_into_loss))
                loss_percent = smart_loss_rate * loss_multiplier

                # Вычисляем цену закрытия с убытком
                if is_long:
                    smart_loss_price = entry_price * (1 - loss_percent / 100)
                else:  # SHORT
                    smart_loss_price = entry_price * (1 + loss_percent / 100)

                # Проверяем, достигнута ли цена Smart Loss
                smart_loss_triggered = False
                if is_long and low_price <= smart_loss_price:
                    smart_loss_triggered = True
                elif is_short and high_price >= smart_loss_price:
                    smart_loss_triggered = True

                if smart_loss_triggered:
                    is_closed = True
                    close_reason = 'smart_loss'
                    close_price = smart_loss_price
                    close_time = candle_time
                    print(f"[PHASE 3] Закрытие по Smart Loss: {close_price:.8f} (убыток {loss_percent:.2f}%)")
                    break

    # КРИТИЧНО: Если позиция все еще открыта - закрываем с data_end (как в check_wr_final.py:321-323)
    if not is_closed and history and len(history) > 0:
        last_candle = history[-1]
        is_closed = True
        close_reason = 'data_end'
        close_price = float(last_candle['close_price'])
        close_time = last_candle['timestamp']
        duration_hours = (close_time - signal_timestamp).total_seconds() / 3600 if signal_timestamp else 0
        print(f"[DEBUG CALC_TS] ❌ Closed as data_end at {close_time} after {duration_hours:.2f}h (all history processed, no exit triggered)")
        print(f"[DEBUG CALC_TS] phase2_forced_entry was: {phase2_forced_entry}, is_trailing_active was: {is_trailing_active}")

    # Формируем результат
    if is_closed:
        if is_short:
            result['pnl_percent'] = ((entry_price - close_price) / entry_price) * 100
        else:
            result['pnl_percent'] = ((close_price - entry_price) / entry_price) * 100

        # Расчет PnL с учетом комиссий
        gross_pnl = effective_position * (result['pnl_percent'] / 100)
        net_pnl = gross_pnl - total_commission

        # Применяем ограничение isolated margin
        # КРИТИЧНО: При isolated margin максимальный убыток = начальная маржа + все комиссии
        # Начальная маржа = position_size (не умноженная на leverage)
        # Максимальный убыток = -(position_size + entry_commission + exit_commission)
        max_loss = -(position_size + entry_commission + exit_commission)
        if net_pnl < max_loss:
            print(f"[ISOLATED MARGIN CAP] Capping loss from ${net_pnl:.2f} to ${max_loss:.2f} (position: ${position_size}, entry_fee: ${entry_commission:.2f}, exit_fee: ${exit_commission:.2f})")
            net_pnl = max_loss

        result['pnl_usd'] = net_pnl  # NET PnL
        result['gross_pnl_usd'] = gross_pnl
        result['commission_usd'] = total_commission
        result['entry_commission_usd'] = entry_commission
        result['exit_commission_usd'] = exit_commission
        result['is_closed'] = True
        result['close_reason'] = close_reason
        result['close_price'] = close_price
        result['close_time'] = close_time

    # Сохраняем максимальный профит независимо от закрытия
    result['max_profit_usd'] = max_profit_usd
    result['best_price'] = best_price_for_trailing
    result['absolute_best_price'] = absolute_best_price

    # Сохраняем историю и entry_price для force_close_all_positions
    result['history'] = history
    result['entry_price'] = entry_price

    # Отладочная информация
    if not is_closed:
        print(f"[TRAILING] Позиция остается открытой:")
        print(f"  Entry: {entry_price:.8f}")
        print(f"  Best price: {best_price_for_trailing:.8f}")
        print(f"  Activation price: {activation_price:.8f}")
        print(f"  Trailing active: {is_trailing_active}")
        if is_trailing_active:
            print(f"  Current stop: {trailing_stop_price:.8f}")

    return result


def process_signal_with_trailing(db, signal, user_settings):
    """
    Обработка сигнала с учетом пользовательских настроек (Fixed TP/SL или Trailing Stop)
    """
    try:
        signal_id = signal['signal_id']
        trading_pair_id = signal['trading_pair_id']
        pair_symbol = signal['pair_symbol']
        signal_action = signal['signal_action']
        signal_timestamp = signal['signal_timestamp']
        exchange_name = signal.get('exchange_name', 'Unknown')
        signal_timestamp = make_aware(signal_timestamp)
        
        # Проверяем наличие открытых позиций по этой паре
        open_position = has_open_position(db, pair_symbol)
        if open_position:
            logger.warning(f"[DUPLICATE SKIPPED] Пропускаем сигнал {signal_id} для {pair_symbol} - уже есть открытая позиция (signal_id={open_position['signal_id']})")
            return {'success': False, 'reason': 'duplicate_position', 'existing_signal_id': open_position['signal_id']}

        # Извлекаем настройки
        use_trailing = user_settings.get('use_trailing_stop', False)
        tp_percent = float(user_settings.get('take_profit_percent', Config.DEFAULT_TAKE_PROFIT_PERCENT))
        sl_percent = float(user_settings.get('stop_loss_percent', Config.DEFAULT_STOP_LOSS_PERCENT))
        trailing_distance = float(user_settings.get('trailing_distance_pct', Config.DEFAULT_TRAILING_DISTANCE_PCT))
        trailing_activation = float(user_settings.get('trailing_activation_pct', Config.DEFAULT_TRAILING_ACTIVATION_PCT))
        position_size = float(user_settings.get('position_size_usd', Config.DEFAULT_POSITION_SIZE))
        leverage = int(user_settings.get('leverage', Config.DEFAULT_LEVERAGE))

        # Получаем цену входа (используем helper function для миграции на public.candles)
        entry_price_query = build_entry_price_query(window_minutes=5)
        ts_param = convert_timestamp_param(signal_timestamp)

        price_result = db.execute_query(
            entry_price_query,
            (trading_pair_id, ts_param, ts_param, ts_param),
            fetch=True
        )

        if not price_result:
            # Расширенный поиск (используем helper function)
            fallback_query = build_entry_price_fallback_query(window_hours=1)
            price_result = db.execute_query(
                fallback_query,
                (trading_pair_id, ts_param, ts_param, ts_param),
                fetch=True
            )

        if not price_result:
            return {'success': False, 'error': 'No price data'}

        entry_price = float(price_result[0]['entry_price'])

        # Получаем историю (24 часа от signal_timestamp для полной симуляции)
        # Используем helper function для миграции на public.candles
        history_query = build_candle_history_query(duration_hours=24)
        start_ts = convert_timestamp_param(signal_timestamp)

        # Для public.candles: end_ts = start_ts + 24h в миллисекундах
        from datetime import timedelta
        if Config.USE_PUBLIC_CANDLES:
            end_ts = start_ts + (24 * 60 * 60 * 1000)  # +24 часа в миллисекундах
        else:
            end_ts = signal_timestamp

        history = db.execute_query(history_query, (trading_pair_id, start_ts, end_ts), fetch=True)

        if not history:
            return {'success': False, 'error': 'No history data'}

        # ВЫБОР ЛОГИКИ: Trailing Stop или Fixed TP/SL
        if use_trailing:
            # Вычисляем simulation_end_time (24 часа от signal_timestamp)
            from datetime import timedelta
            simulation_end_time = signal_timestamp + timedelta(hours=24) if signal_timestamp else None

            # Используем trailing stop
            result = calculate_trailing_stop_exit(
                entry_price, history, signal_action,
                trailing_distance, trailing_activation,
                sl_percent, position_size, leverage,
                signal_timestamp,  # Передаем timestamp для таймаута
                Config.DEFAULT_COMMISSION_RATE,  # Передаем commission_rate
                simulation_end_time  # КРИТИЧНО: передаем simulation_end_time!
            )

            is_closed = result['is_closed']
            close_price = result['close_price']
            close_time = result['close_time']
            close_reason = result['close_reason']
            realized_pnl = result['pnl_usd'] if is_closed else 0
            max_profit = result['max_profit_usd']
            trailing_activated = result.get('trailing_activated', False)

        else:
            # СУЩЕСТВУЮЩАЯ ЛОГИКА Fixed TP/SL
            max_profit = 0
            is_closed = False
            close_price = None
            close_time = None
            close_reason = None
            realized_pnl = 0
            best_price_ever = entry_price
            trailing_activated = False

            # Рассчитываем уровни TP и SL
            if signal_action in ['SELL', 'SHORT']:
                tp_price = entry_price * (1 - tp_percent / 100)
                sl_price = entry_price * (1 + sl_percent / 100)
            else:  # BUY, LONG
                tp_price = entry_price * (1 + tp_percent / 100)
                sl_price = entry_price * (1 - sl_percent / 100)

            # Проходим по истории
            for candle in history:
                current_time = candle['timestamp']
                high_price = float(candle['high_price'])
                low_price = float(candle['low_price'])

                # Обновляем максимальный профит
                if signal_action in ['SELL', 'SHORT']:
                    if low_price < best_price_ever:
                        best_price_ever = low_price
                        max_profit_percent = ((entry_price - best_price_ever) / entry_price) * 100
                        potential_max_profit = position_size * (max_profit_percent / 100) * leverage
                        if potential_max_profit > max_profit:
                            max_profit = potential_max_profit

                    # Проверка закрытия для SHORT
                    if not is_closed:
                        # Проверка ликвидации (приоритет!)
                        liquidation_threshold = Config.LIQUIDATION_THRESHOLD
                        liquidation_loss_pct = -(100 / leverage) * liquidation_threshold
                        unrealized_pnl_pct = ((entry_price - high_price) / entry_price) * 100
                        if unrealized_pnl_pct <= liquidation_loss_pct:
                            is_closed = True
                            close_reason = 'liquidation'
                            # КРИТИЧНО: Ликвидация по liquidation_price, не по actual price
                            close_price = entry_price * (1 - liquidation_loss_pct / 100)
                            close_time = current_time
                        elif low_price <= tp_price:
                            is_closed = True
                            close_reason = 'take_profit'
                            close_price = tp_price
                            close_time = current_time
                        elif high_price >= sl_price:
                            is_closed = True
                            close_reason = 'stop_loss'
                            close_price = sl_price
                            close_time = current_time
                else:  # BUY, LONG
                    if high_price > best_price_ever:
                        best_price_ever = high_price
                        max_profit_percent = ((best_price_ever - entry_price) / entry_price) * 100
                        potential_max_profit = position_size * (max_profit_percent / 100) * leverage
                        if potential_max_profit > max_profit:
                            max_profit = potential_max_profit

                    # Проверка закрытия для LONG
                    if not is_closed:
                        # Проверка ликвидации (приоритет!)
                        liquidation_threshold = Config.LIQUIDATION_THRESHOLD
                        liquidation_loss_pct = -(100 / leverage) * liquidation_threshold
                        unrealized_pnl_pct = ((low_price - entry_price) / entry_price) * 100
                        if unrealized_pnl_pct <= liquidation_loss_pct:
                            is_closed = True
                            close_reason = 'liquidation'
                            # КРИТИЧНО: Ликвидация по liquidation_price, не по actual price
                            close_price = entry_price * (1 + liquidation_loss_pct / 100)
                            close_time = current_time
                        elif high_price >= tp_price:
                            is_closed = True
                            close_reason = 'take_profit'
                            close_price = tp_price
                            close_time = current_time
                        elif low_price <= sl_price:
                            is_closed = True
                            close_reason = 'stop_loss'
                            close_price = sl_price
                            close_time = current_time

        # Последняя известная цена
        last_price = float(history[-1]['close_price'])

        # Если не закрылась, проверяем таймаут (24 часа)
        if not is_closed:
            hours_passed = (history[-1]['timestamp'] - signal_timestamp).total_seconds() / 3600
            if hours_passed >= 24:
                is_closed = True
                close_reason = 'timeout'
                close_price = last_price
                close_time = history[-1]['timestamp']

        # Рассчитываем финальный PnL (если use_trailing=False, т.к. для trailing это уже сделано в calculate_trailing_stop_exit)
        if not use_trailing and is_closed:
            # Рассчитываем комиссии для Fixed TP/SL режима
            commission_rate = Config.DEFAULT_COMMISSION_RATE
            effective_position = position_size * leverage
            entry_commission = effective_position * commission_rate
            exit_commission = effective_position * commission_rate
            total_commission = entry_commission + exit_commission

            if signal_action in ['SELL', 'SHORT']:
                pnl_percent = ((entry_price - close_price) / entry_price) * 100
            else:
                pnl_percent = ((close_price - entry_price) / entry_price) * 100

            gross_pnl = effective_position * (pnl_percent / 100)
            realized_pnl = gross_pnl - total_commission  # NET PnL после комиссий

            # Применяем ограничение isolated margin
            max_loss = -(position_size + entry_commission + exit_commission)
            if realized_pnl < max_loss:
                print(f"[ISOLATED MARGIN CAP] Capping loss from ${realized_pnl:.2f} to ${max_loss:.2f} (position: ${position_size}, fees: ${total_commission:.2f})")
                realized_pnl = max_loss
        elif not is_closed:
            realized_pnl = 0

        # Рассчитываем unrealized PnL
        if not is_closed:
            if signal_action in ['SELL', 'SHORT']:
                unrealized_percent = ((entry_price - last_price) / entry_price) * 100
            else:
                unrealized_percent = ((last_price - entry_price) / entry_price) * 100
            unrealized_pnl = position_size * (unrealized_percent / 100) * leverage
        else:
            unrealized_pnl = 0

        # Сохраняем в БД (добавляем новые поля)
        # Получаем exchange_id из signal
        exchange_id = signal.get('exchange_id')

        insert_query = """
            INSERT INTO web.web_signals (
                signal_id, pair_symbol, signal_action, signal_timestamp,
                entry_price, position_size_usd, leverage,
                trailing_stop_percent, take_profit_percent,
                is_closed, closing_price, closed_at, close_reason,
                realized_pnl_usd, unrealized_pnl_usd,
                max_potential_profit_usd, last_known_price,
                score_week, score_month, exchange_id
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        db.execute_query(insert_query, (
            signal_id, pair_symbol, signal_action, signal_timestamp,
            entry_price, position_size, leverage,
            trailing_distance if use_trailing else sl_percent,
            trailing_activation if use_trailing else tp_percent,
            is_closed, close_price, close_time, close_reason,
            realized_pnl if is_closed else 0,
            unrealized_pnl if not is_closed else 0,
            max_profit, last_price,
            signal.get('score_week', 0), signal.get('score_month', 0), exchange_id
        ))

        return {
            'success': True,
            'is_closed': is_closed,
            'close_reason': close_reason,
            'max_profit': max_profit,
            'realized_pnl': realized_pnl if is_closed else 0,
            'trailing_activated': trailing_activated
        }

    except Exception as e:
        print(f"[PROCESS] Ошибка для сигнала {signal.get('signal_id', 'unknown')}: {e}")
        import traceback
        traceback.print_exc()
        return {'success': False, 'error': str(e)}


def initialize_signals_with_trailing(db, hours_back=None, user_id=None):
    """
    Инициализация сигналов с учетом выбранного режима пользователя.
    Использует Config для дефолтных значений
    """
    if hours_back is None:
        hours_back = Config.DEFAULT_HIDE_OLDER_HOURS
    try:
        # Получаем настройки пользователя
        settings_query = """
            SELECT * FROM web.user_signal_filters WHERE user_id = %s
        """
        settings = db.execute_query(settings_query, (user_id,), fetch=True)

        if not settings:
            # Настройки по умолчанию
            user_settings = {
                'use_trailing_stop': False,
                'take_profit_percent': Config.DEFAULT_TAKE_PROFIT_PERCENT,
                'stop_loss_percent': Config.DEFAULT_STOP_LOSS_PERCENT,
                'trailing_distance_pct': Config.DEFAULT_TRAILING_DISTANCE_PCT,
                'trailing_activation_pct': Config.DEFAULT_TRAILING_ACTIVATION_PCT,
                'position_size_usd': Config.DEFAULT_POSITION_SIZE,
                'leverage': Config.DEFAULT_LEVERAGE
            }
        else:
            s = settings[0]
            user_settings = {
                'use_trailing_stop': s.get('use_trailing_stop', False),
                'take_profit_percent': float(s.get('take_profit_percent', 4.0)),
                'stop_loss_percent': float(s.get('stop_loss_percent', 3.0)),
                'trailing_distance_pct': float(s.get('trailing_distance_pct', 2.0)),
                'trailing_activation_pct': float(s.get('trailing_activation_pct', 1.0)),
                'position_size_usd': float(s.get('position_size_usd', 100.0)),
                'leverage': int(s.get('leverage', 5))
            }

        print(f"[INIT] ========== НАЧАЛО ИНИЦИАЛИЗАЦИИ ==========")
        print(f"[INIT] Режим: {'Trailing Stop' if user_settings['use_trailing_stop'] else 'Fixed TP/SL'}")
        print(f"[INIT] Параметры: ", end="")
        if user_settings['use_trailing_stop']:
            print(f"Distance={user_settings['trailing_distance_pct']}%, "
                  f"Activation={user_settings['trailing_activation_pct']}%, "
                  f"Insurance SL={user_settings['stop_loss_percent']}%")
        else:
            print(f"TP={user_settings['take_profit_percent']}%, "
                  f"SL={user_settings['stop_loss_percent']}%")

        # Очистка таблицы
        print("[INIT] Очистка таблицы web_signals...")
        db.execute_query("TRUNCATE TABLE web.web_signals")

        # Получаем сигналы
        signals_query = """
            SELECT 
                pr.signal_id,
                sh.pair_symbol,
                sh.trading_pair_id,
                pr.signal_type as signal_action,
                pr.created_at as signal_timestamp,
                tp.exchange_id,
                ex.exchange_name
            FROM smart_ml.predictions pr
            JOIN fas_v2.scoring_history sh ON sh.id = pr.signal_id
            JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
            JOIN public.exchanges ex ON ex.id = tp.exchange_id
            WHERE pr.created_at >= NOW() - (INTERVAL '1 hour' * %s)
                AND pr.prediction = true
                AND tp.contract_type_id = 1
                AND tp.exchange_id IN (1, 2)
            ORDER BY pr.created_at DESC
        """

        signals = db.execute_query(signals_query, (hours_back,), fetch=True)
        print(f"[INIT] Найдено {len(signals) if signals else 0} сигналов")

        if not signals:
            return {'initialized': 0, 'closed_tp': 0, 'closed_sl': 0,
                    'closed_trailing': 0, 'open': 0, 'errors': 0}

        stats = {
            'initialized': 0,
            'closed_tp': 0,
            'closed_sl': 0,
            'closed_trailing': 0,
            'open': 0,
            'errors': 0,
            'trailing_activated': 0,
            'by_exchange': {'Binance': 0, 'Bybit': 0}
        }

        # Обрабатываем сигналы
        for idx, signal in enumerate(signals):
            try:
                signal_with_exchange = dict(signal)
                signal_with_exchange['signal_timestamp'] = make_aware(signal_with_exchange['signal_timestamp'])

                result = process_signal_with_trailing(db, signal, user_settings)

                if result.get('success'):
                    stats['initialized'] += 1
                    stats['by_exchange'][signal.get('exchange_name', 'Unknown')] += 1

                    if result.get('is_closed'):
                        if result['close_reason'] == 'take_profit':
                            stats['closed_tp'] += 1
                        elif result['close_reason'] == 'stop_loss':
                            stats['closed_sl'] += 1
                        elif result['close_reason'] == 'trailing_stop':
                            stats['closed_trailing'] += 1
                            # ВАЖНО: trailing_stop с прибылью учитывается как победа
                            if result.get('pnl_usd', 0) > 0:
                                stats['closed_tp'] += 1  # Добавляем к победным
                    else:
                        stats['open'] += 1

                    # Для trailing mode считаем активации
                    if user_settings['use_trailing_stop'] and result.get('trailing_activated'):
                        stats['trailing_activated'] += 1
                elif result.get('reason') == 'duplicate_position':
                    # Учитываем пропущенные дубликаты
                    if 'duplicates_skipped' not in stats:
                        stats['duplicates_skipped'] = 0
                    stats['duplicates_skipped'] += 1
                    print(f"[INIT] Пропущен дубликат для {signal.get('pair_symbol', 'unknown')} - уже есть открытая позиция")
                else:
                    stats['errors'] += 1

                if idx % 100 == 0 and idx > 0:
                    print(f"[INIT] Обработано {idx}/{len(signals)} сигналов...")

            except Exception as e:
                print(f"[INIT] Ошибка при обработке сигнала {signal.get('signal_id', 'unknown')}: {e}")
                stats['errors'] += 1
                continue

        print(f"[INIT] ========== РЕЗУЛЬТАТЫ ==========")
        print(f"[INIT] Инициализировано: {stats['initialized']}")
        print(f"[INIT]   - Binance: {stats['by_exchange']['Binance']}")
        print(f"[INIT]   - Bybit: {stats['by_exchange']['Bybit']}")
        print(f"[INIT] Пропущено дубликатов: {stats.get('duplicates_skipped', 0)}")
        if user_settings['use_trailing_stop']:
            print(f"[INIT] Trailing активирован: {stats['trailing_activated']}")
            print(f"[INIT] Закрыто по trailing: {stats['closed_trailing']}")
        print(f"[INIT] Закрыто по TP: {stats['closed_tp']}")
        print(f"[INIT] Закрыто по SL: {stats['closed_sl']}")
        print(f"[INIT] Остались открытыми: {stats['open']}")
        print(f"[INIT] Ошибок: {stats['errors']}")

        return stats

    except Exception as e:
        print(f"[INIT] КРИТИЧЕСКАЯ ОШИБКА: {e}")
        import traceback
        traceback.print_exc()
        return {'initialized': 0, 'errors': 1}


# ========== ФУНКЦИИ ДЛЯ РАЗДЕЛА СКОРИНГ ==========

def get_scoring_date_range(db):
    """Получение диапазона дат для фильтра скоринга"""
    query = """
        SELECT 
            MIN(timestamp)::date as min_date,
            (CURRENT_DATE - INTERVAL '2 days')::date as max_date
        FROM fas_v2.scoring_history
    """
    result = db.execute_query(query, fetch=True)
    return result[0] if result else {'min_date': None, 'max_date': None}


def get_scoring_signals(db, date_filter, score_week_min=None, score_month_min=None, allowed_hours=None):
    """
    Получение сигналов на основе простых фильтров score_week, score_month и allowed_hours
    НОВАЯ ЛОГИКА: Прямой запрос к fas_v2.scoring_history без сложных конструкторов
    """

    print(f"\n[SCORING] ========== ПОЛУЧЕНИЕ СИГНАЛОВ ==========")
    print(f"[SCORING] Дата: {date_filter}")
    print(f"[SCORING] Минимальный score_week: {score_week_min}")
    print(f"[SCORING] Минимальный score_month: {score_month_min}")
    
    if allowed_hours is None or len(allowed_hours) == 0:
        print(f"[SCORING] Разрешенные часы: Не заданы (фильтр не применяется)")
    elif len(allowed_hours) == 24:
        print(f"[SCORING] Разрешенные часы: Все 24 часа (фильтр не применяется)")
    else:
        print(f"[SCORING] Разрешенные часы (UTC): {sorted(allowed_hours)}")
        print(f"[SCORING] Количество разрешенных часов: {len(allowed_hours)}")

    # Базовый запрос к fas_v2.scoring_history
    query = """
        WITH market_regime_data AS (
            -- Получаем режимы рынка для выбранной даты
            SELECT DISTINCT ON (DATE_TRUNC('hour', timestamp))
                DATE_TRUNC('hour', timestamp) as hour_bucket,
                regime,
                timestamp
            FROM fas_v2.market_regime
            WHERE timeframe = '4h'
                AND timestamp::date = %s
            ORDER BY DATE_TRUNC('hour', timestamp), timestamp DESC
        )
        SELECT
            sh.id as signal_id,
            sh.timestamp,
            sh.trading_pair_id,
            sh.pair_symbol,
            sh.recommended_action as signal_action,
            sh.total_score,
            sh.indicator_score,
            sh.pattern_score,
            sh.combination_score,
            sh.score_week,
            sh.score_month,
            tp.exchange_id,
            ex.exchange_name,
            COALESCE(mr.regime, 'NEUTRAL') AS market_regime
        FROM fas_v2.scoring_history sh
        JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
        JOIN public.exchanges ex ON ex.id = tp.exchange_id
        LEFT JOIN LATERAL (
            SELECT regime
            FROM market_regime_data mr
            WHERE mr.hour_bucket <= DATE_TRUNC('hour', sh.timestamp)
            ORDER BY mr.hour_bucket DESC
            LIMIT 1
        ) AS mr ON true
        WHERE sh.timestamp::date = %s
            AND tp.contract_type_id = 1
            AND tp.exchange_id IN (1, 2)
    """

    params = [date_filter, date_filter]

    # Добавляем фильтр по score_week если задан
    if score_week_min is not None:
        query += " AND sh.score_week >= %s"
        params.append(score_week_min)

    # Добавляем фильтр по score_month если задан
    if score_month_min is not None:
        query += " AND sh.score_month >= %s"
        params.append(score_month_min)

    # Добавляем фильтр по разрешенным часам если задан
    # Применяем фильтр только если выбраны НЕ все часы (не 24 часа)
    if allowed_hours is not None and len(allowed_hours) > 0 and len(allowed_hours) < 24:
        query += " AND EXTRACT(hour FROM sh.timestamp AT TIME ZONE 'UTC')::integer = ANY(%s::integer[])"
        params.append(allowed_hours)

    query += " ORDER BY sh.timestamp DESC"

    try:
        results = db.execute_query(query, tuple(params), fetch=True)

        if results:
            print(f"[SCORING] Найдено сигналов: {len(results)}")

            # Группируем по биржам и часам для статистики
            exchanges_count = {}
            actions_count = {'BUY': 0, 'SELL': 0, 'LONG': 0, 'SHORT': 0, 'NEUTRAL': 0}
            hours_count = {}

            for signal in results:
                exchange = signal.get('exchange_name', 'Unknown')
                exchanges_count[exchange] = exchanges_count.get(exchange, 0) + 1

                action = signal.get('signal_action', 'NEUTRAL')
                if action in actions_count:
                    actions_count[action] += 1
                    
                # Добавляем статистику по часам
                signal_timestamp = signal.get('timestamp')
                if signal_timestamp:
                    signal_hour = signal_timestamp.hour
                    hours_count[signal_hour] = hours_count.get(signal_hour, 0) + 1

            print("\n[SCORING] Распределение по биржам:")
            for exchange, count in exchanges_count.items():
                print(f"  {exchange}: {count} сигналов")
            
            if allowed_hours and hours_count:
                print("\n[SCORING] Распределение по часам (UTC):")
                for hour in sorted(hours_count.keys()):
                    print(f"  {hour:02d}:00 - {hours_count[hour]} сигналов")
                    
                # Проверяем, есть ли сигналы вне разрешенных часов
                invalid_hours = [h for h in hours_count.keys() if h not in allowed_hours]
                if invalid_hours:
                    print(f"\n[SCORING] ВНИМАНИЕ! Найдены сигналы в неразрешенные часы: {invalid_hours}")
                    print(f"[SCORING] Это может указывать на проблему с фильтром")

            print("\n[SCORING] Распределение по типам сигналов:")
            for action, count in actions_count.items():
                if count > 0:
                    print(f"  {action}: {count}")
        else:
            print("[SCORING] Сигналов не найдено")

        return results if results else []

    except Exception as e:
        print(f"[SCORING] ОШИБКА выполнения запроса: {e}")
        import traceback
        print(traceback.format_exc())
        return []


def get_scoring_date_info(db, date, score_week_min=None, score_month_min=None, allowed_hours=None):
    """
    Получение информации о выбранной дате: режимы рынка и количество сигналов с учетом разрешенных часов
    """
    result = {
        'market_regimes': {'BULL': 0, 'NEUTRAL': 0, 'BEAR': 0},
        'dominant_regime': 'NEUTRAL',
        'signal_count': 0
    }

    try:
        # Получаем режимы рынка для этой даты
        market_query = """
            SELECT DISTINCT 
                DATE_TRUNC('hour', timestamp) as hour,
                regime
            FROM fas_v2.market_regime
            WHERE timestamp::date = %s
                AND timeframe = '4h'
            ORDER BY hour
        """
        market_data = db.execute_query(market_query, (date,), fetch=True)

        # Подсчитываем распределение режимов
        for row in market_data:
            regime = row['regime']
            if regime in result['market_regimes']:
                result['market_regimes'][regime] += 1

        # Определяем доминирующий режим
        if result['market_regimes']:
            result['dominant_regime'] = max(result['market_regimes'],
                                            key=result['market_regimes'].get)

        # Подсчитываем количество сигналов с учетом фильтров
        count_query = """
            SELECT COUNT(*) as count
            FROM fas_v2.scoring_history sh
            JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
            WHERE sh.timestamp::date = %s
                AND tp.contract_type_id = 1
                AND tp.exchange_id IN (1, 2)
        """

        params = [date]

        if score_week_min is not None:
            count_query += " AND sh.score_week >= %s"
            params.append(score_week_min)

        if score_month_min is not None:
            count_query += " AND sh.score_month >= %s"
            params.append(score_month_min)

        # Добавляем фильтр по разрешенным часам если задан
        if allowed_hours is not None and allowed_hours:
            count_query += " AND EXTRACT(hour FROM sh.timestamp AT TIME ZONE 'UTC')::integer = ANY(%s::integer[])"
            params.append(allowed_hours)

        count_result = db.execute_query(count_query, tuple(params), fetch=True)
        if count_result:
            result['signal_count'] = count_result[0]['count']

    except Exception as e:
        print(f"[SCORING] Ошибка получения информации о дате: {e}")

    return result


def group_signals_by_wave(signals, wave_interval_minutes=15):
    """
    Группирует сигналы по 15-минутным волнам

    Args:
        signals: Список сигналов
        wave_interval_minutes: Интервал волны в минутах (по умолчанию 15)

    Returns:
        dict: {wave_time: [signals]} - сигналы, сгруппированные и отсортированные по score
    """
    from collections import defaultdict

    signals_by_wave = defaultdict(list)

    for signal in signals:
        ts = signal['timestamp']
        # Округляем до границы волны (например, 15 минут)
        minute_rounded = (ts.minute // wave_interval_minutes) * wave_interval_minutes
        wave_key = ts.replace(minute=minute_rounded, second=0, microsecond=0)
        signals_by_wave[wave_key].append(signal)

    # Сортируем сигналы внутри каждой волны по score_week (лучшие первыми)
    for wave_key in signals_by_wave:
        signals_by_wave[wave_key].sort(
            key=lambda x: x.get('score_week', 0),
            reverse=True  # От большего к меньшему
        )

    return signals_by_wave


def process_scoring_signals_batch(db, signals, session_id, user_id,
                                  tp_percent=None, sl_percent=None,
                                  position_size=None, leverage=None,
                                  use_trailing_stop=None,
                                  trailing_distance_pct=None,
                                  trailing_activation_pct=None):
    """
    Обработка пакета сигналов для scoring анализа
    Использует дефолтные значения из Config если параметры не переданы
    """
    # Использовать значения из Config если не переданы  
    tp_percent = tp_percent if tp_percent is not None else Config.DEFAULT_TAKE_PROFIT_PERCENT
    sl_percent = sl_percent if sl_percent is not None else Config.DEFAULT_STOP_LOSS_PERCENT
    position_size = position_size if position_size is not None else Config.DEFAULT_POSITION_SIZE
    leverage = leverage if leverage is not None else Config.DEFAULT_LEVERAGE
    use_trailing_stop = use_trailing_stop if use_trailing_stop is not None else Config.DEFAULT_USE_TRAILING_STOP
    trailing_distance_pct = trailing_distance_pct if trailing_distance_pct is not None else Config.DEFAULT_TRAILING_DISTANCE_PCT
    trailing_activation_pct = trailing_activation_pct if trailing_activation_pct is not None else Config.DEFAULT_TRAILING_ACTIVATION_PCT
    
    # Пакетная обработка сигналов скоринга с поддержкой Trailing Stop

    # Очищаем предыдущие результаты для этой сессии
    clear_query = """
        DELETE FROM web.scoring_analysis_results 
        WHERE session_id = %s AND user_id = %s
    """
    db.execute_query(clear_query, (session_id, user_id))

    print(f"[SCORING] Начинаем обработку {len(signals)} сигналов...")
    print(f"[SCORING] Режим: {'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'}")
    print(f"[SCORING] Параметры: TP={tp_percent}%, SL={sl_percent}%, Size=${position_size}, Lev={leverage}x")
    if use_trailing_stop:
        print(f"[SCORING] Trailing: Activation={trailing_activation_pct}%, Distance={trailing_distance_pct}%")

    processed_count = 0
    error_count = 0
    exchange_stats = {'Binance': 0, 'Bybit': 0, 'errors': {}}

    # Счетчики для отладки
    debug_stats = {
        'total': 0,
        'trailing_activated': 0,
        'closed_trailing': 0,
        'closed_tp': 0,
        'closed_sl': 0,
        'closed_timeout': 0,
        'still_open': 0
    }

    # Подготавливаем данные для batch insert
    batch_data = []

    for idx, signal in enumerate(signals):
        try:
            exchange_name = signal.get('exchange_name', 'Unknown')

            # ИСПРАВЛЕНИЕ: Используем правильное имя поля
            pair_symbol = signal.get('pair_symbol')  # Изменено с 'symbol' на 'pair_symbol'
            if not pair_symbol:
                print(f"[SCORING] Предупреждение: отсутствует pair_symbol для сигнала {signal.get('signal_id')}")
                error_count += 1
                continue

            # Получаем цену входа (используем helper function, 15-min window для scoring)
            entry_price_query = build_entry_price_query(window_minutes=15)
            ts_param = convert_timestamp_param(signal['timestamp'])

            price_result = db.execute_query(
                entry_price_query,
                (signal['trading_pair_id'], ts_param, ts_param, ts_param),
                fetch=True
            )

            if not price_result:
                # Расширенный поиск (используем helper function)
                extended_query = build_entry_price_fallback_query(window_hours=1)
                price_result = db.execute_query(
                    extended_query,
                    (signal['trading_pair_id'], ts_param, ts_param, ts_param),
                    fetch=True
                )

                if not price_result:
                    error_count += 1
                    continue

            entry_price = float(price_result[0]['entry_price'])

            # Получаем историю (используем helper function)
            history_query = build_candle_history_query(duration_hours=24)
            start_ts = convert_timestamp_param(signal['timestamp'])

            # Для public.candles: end_ts = start_ts + 24h в миллисекундах
            if Config.USE_PUBLIC_CANDLES:
                end_ts = start_ts + (24 * 60 * 60 * 1000)  # +24 часа в миллисекундах
            else:
                end_ts = signal['timestamp']

            history = db.execute_query(
                history_query,
                (signal['trading_pair_id'], start_ts, end_ts),
                fetch=True
            )

            if not history or len(history) < 2:
                error_count += 1
                continue

            debug_stats['total'] += 1

            # ВЫБОР ЛОГИКИ: Trailing Stop или Fixed TP/SL
            if use_trailing_stop:
                # Вычисляем simulation_end_time для этого конкретного сигнала
                signal_simulation_end_time = signal['timestamp'] + timedelta(hours=24) if signal.get('timestamp') else simulation_end_time

                # Используем функцию trailing stop с передачей timestamp
                result = calculate_trailing_stop_exit(
                    entry_price,
                    history,
                    signal['signal_action'],
                    trailing_distance_pct,
                    trailing_activation_pct,
                    sl_percent,
                    position_size,
                    leverage,
                    signal_timestamp=signal['timestamp'],
                    commission_rate=Config.DEFAULT_COMMISSION_RATE,
                    simulation_end_time=signal_simulation_end_time  # КРИТИЧНО: передаем simulation_end_time!
                )

                # Извлекаем все данные из результата
                is_closed = result['is_closed']
                close_price = result['close_price']
                close_time = result['close_time']
                close_reason = result['close_reason']
                best_price_reached = result['absolute_best_price']
                max_profit_usd = result['max_profit_usd']

                # Рассчитываем финальный P&L
                if is_closed:
                    final_pnl_percent = result['pnl_percent']
                    final_pnl_usd = result['pnl_usd']
                    hours_to_close = (close_time - signal['timestamp']).total_seconds() / 3600 if close_time else None
                else:
                    # Для открытых позиций
                    last_price = float(history[-1]['close_price'])
                    close_price = last_price
                    close_time = history[-1]['timestamp']

                    if signal['signal_action'] in ['SELL', 'SHORT']:
                        final_pnl_percent = ((entry_price - last_price) / entry_price) * 100
                    else:
                        final_pnl_percent = ((last_price - entry_price) / entry_price) * 100
                    final_pnl_usd = position_size * (final_pnl_percent / 100) * leverage
                    hours_to_close = 48.0

                # Расчет max_profit_percent для сохранения
                max_profit_percent = (max_profit_usd / (position_size * leverage)) * 100 if (
                                                                                                        position_size * leverage) > 0 else 0

                # Обновляем счетчики для отладки
                if result.get('trailing_activated', False):
                    debug_stats['trailing_activated'] += 1

                if is_closed:
                    if close_reason == 'trailing_stop':
                        debug_stats['closed_trailing'] += 1
                    elif close_reason == 'stop_loss':
                        debug_stats['closed_sl'] += 1
                    elif close_reason == 'timeout':
                        debug_stats['closed_timeout'] += 1
                else:
                    debug_stats['still_open'] += 1

            else:
                # СУЩЕСТВУЮЩАЯ ЛОГИКА Fixed TP/SL с учетом комиссий
                # Расчет комиссий
                commission_rate = Config.DEFAULT_COMMISSION_RATE
                effective_position = position_size * leverage
                entry_commission = effective_position * commission_rate
                exit_commission = effective_position * commission_rate
                total_commission = entry_commission + exit_commission

                is_closed = False
                close_reason = None
                close_price = None
                close_time = None
                hours_to_close = None
                max_profit_percent = 0
                max_profit_usd = 0
                best_price_reached = entry_price

                # Рассчитываем уровни TP и SL
                if signal['signal_action'] in ['SELL', 'SHORT']:
                    tp_price = entry_price * (1 - tp_percent / 100)
                    sl_price = entry_price * (1 + sl_percent / 100)
                else:  # BUY
                    tp_price = entry_price * (1 + tp_percent / 100)
                    sl_price = entry_price * (1 - sl_percent / 100)

                # Проходим по истории
                for candle in history:
                    current_time = candle['timestamp']
                    hours_passed = (current_time - signal['timestamp']).total_seconds() / 3600
                    high_price = float(candle['high_price'])
                    low_price = float(candle['low_price'])

                    # Обновляем максимальный достигнутый профит
                    if signal['signal_action'] in ['SELL', 'SHORT']:
                        if low_price < best_price_reached:
                            best_price_reached = low_price
                            temp_profit_percent = ((entry_price - best_price_reached) / entry_price) * 100
                            gross_temp_profit = effective_position * (temp_profit_percent / 100)
                            temp_profit_usd = gross_temp_profit - total_commission  # Вычитаем комиссии
                            if temp_profit_usd > max_profit_usd:
                                max_profit_percent = temp_profit_percent
                                max_profit_usd = temp_profit_usd

                        # Проверка TP/SL для SHORT
                        if not is_closed:
                            # Проверка ликвидации (приоритет!)
                            liquidation_threshold = Config.LIQUIDATION_THRESHOLD
                            liquidation_loss_pct = -(100 / leverage) * liquidation_threshold
                            unrealized_pnl_pct = ((entry_price - high_price) / entry_price) * 100
                            if unrealized_pnl_pct <= liquidation_loss_pct:
                                is_closed = True
                                close_reason = 'liquidation'
                                close_price = high_price
                                close_time = current_time
                                hours_to_close = hours_passed
                            elif low_price <= tp_price:
                                is_closed = True
                                close_reason = 'take_profit'
                                close_price = tp_price
                                close_time = current_time
                                hours_to_close = hours_passed
                                debug_stats['closed_tp'] += 1
                            elif high_price >= sl_price:
                                is_closed = True
                                close_reason = 'stop_loss'
                                close_price = sl_price
                                close_time = current_time
                                hours_to_close = hours_passed
                                debug_stats['closed_sl'] += 1

                    else:  # BUY, LONG
                        if high_price > best_price_reached:
                            best_price_reached = high_price
                            temp_profit_percent = ((best_price_reached - entry_price) / entry_price) * 100
                            gross_temp_profit = effective_position * (temp_profit_percent / 100)
                            temp_profit_usd = gross_temp_profit - total_commission  # Вычитаем комиссии
                            if temp_profit_usd > max_profit_usd:
                                max_profit_percent = temp_profit_percent
                                max_profit_usd = temp_profit_usd

                        # Проверка TP/SL для LONG
                        if not is_closed:
                            # Проверка ликвидации (приоритет!)
                            liquidation_threshold = Config.LIQUIDATION_THRESHOLD
                            liquidation_loss_pct = -(100 / leverage) * liquidation_threshold
                            unrealized_pnl_pct = ((low_price - entry_price) / entry_price) * 100
                            if unrealized_pnl_pct <= liquidation_loss_pct:
                                is_closed = True
                                close_reason = 'liquidation'
                                close_price = low_price
                                close_time = current_time
                                hours_to_close = hours_passed
                            elif high_price >= tp_price:
                                is_closed = True
                                close_reason = 'take_profit'
                                close_price = tp_price
                                close_time = current_time
                                hours_to_close = hours_passed
                                debug_stats['closed_tp'] += 1
                            elif low_price <= sl_price:
                                is_closed = True
                                close_reason = 'stop_loss'
                                close_price = sl_price
                                close_time = current_time
                                hours_to_close = hours_passed
                                debug_stats['closed_sl'] += 1

                # Если не закрылась, проверяем таймаут
                if not is_closed:
                    last_price = float(history[-1]['close_price'])
                    hours_passed = (history[-1]['timestamp'] - signal['timestamp']).total_seconds() / 3600
                    if hours_passed >= 48:
                        is_closed = True
                        close_reason = 'timeout'
                        close_price = last_price
                        close_time = history[-1]['timestamp']
                        hours_to_close = 48.0
                        debug_stats['closed_timeout'] += 1
                    else:
                        # Остается открытой
                        close_price = last_price
                        close_time = history[-1]['timestamp']
                        hours_to_close = hours_passed
                        debug_stats['still_open'] += 1

                # Рассчитываем финальный P&L с учетом комиссий
                if signal['signal_action'] in ['SELL', 'SHORT']:
                    final_pnl_percent = ((entry_price - close_price) / entry_price) * 100
                else:
                    final_pnl_percent = ((close_price - entry_price) / entry_price) * 100
                gross_final_pnl = effective_position * (final_pnl_percent / 100)
                final_pnl_usd = gross_final_pnl - total_commission  # NET PnL после комиссий

            # Добавляем в batch с КОРРЕКТНЫМИ данными
            batch_data.append((
                session_id,
                user_id,
                signal['timestamp'],
                pair_symbol,  # ИСПРАВЛЕНО: используем правильную переменную
                signal['trading_pair_id'],
                signal['signal_action'],
                signal.get('market_regime', 'NEUTRAL'),
                exchange_name,
                float(signal.get('total_score', 0)),
                float(signal.get('indicator_score', 0)),
                float(signal.get('pattern_score', 0)),
                float(signal.get('combination_score', 0)),
                float(signal.get('score_week', 0)),
                float(signal.get('score_month', 0)),
                entry_price,
                best_price_reached,
                close_price,
                close_time,
                is_closed,
                close_reason,
                hours_to_close,
                final_pnl_percent,
                final_pnl_usd,
                max_profit_percent,
                max_profit_usd,
                tp_percent,
                sl_percent,
                position_size,
                leverage
            ))

            processed_count += 1
            exchange_stats[exchange_name] = exchange_stats.get(exchange_name, 0) + 1

            # Вставляем пачками по 50
            if len(batch_data) >= 50:
                _insert_batch_results(db, batch_data)
                batch_data = []
                print(f"[SCORING] Обработано {processed_count}/{len(signals)} сигналов...")

        except Exception as e:
            error_count += 1
            print(f"[SCORING] Ошибка обработки сигнала {signal.get('pair_symbol', 'UNKNOWN')}: {e}")
            import traceback
            traceback.print_exc()
            continue

    # Вставляем оставшиеся данные
    if batch_data:
        _insert_batch_results(db, batch_data)

    # Выводим итоговую статистику
    print(f"\n[SCORING] ===== ИТОГОВАЯ СТАТИСТИКА =====")
    print(f"[SCORING] Обработано сигналов: {debug_stats['total']}")
    if use_trailing_stop:
        print(f"[SCORING] Trailing активирован: {debug_stats['trailing_activated']}")
        print(f"[SCORING] Закрыто по trailing: {debug_stats['closed_trailing']}")
    else:
        print(f"[SCORING] Закрыто по TP: {debug_stats['closed_tp']}")
    print(f"[SCORING] Закрыто по SL: {debug_stats['closed_sl']}")
    print(f"[SCORING] Закрыто по timeout: {debug_stats['closed_timeout']}")
    print(f"[SCORING] Остались открытыми: {debug_stats['still_open']}")
    print(f"[SCORING] Ошибок: {error_count}")

    # Возвращаем статистику из БД
    stats_query = """
            SELECT 
                COUNT(*) as total,
                COUNT(CASE WHEN signal_action = 'BUY' THEN 1 END) as buy_signals,
                COUNT(CASE WHEN signal_action = 'SELL' THEN 1 END) as sell_signals,

                -- ИСПРАВЛЕНО: Корректный подсчет без двойного учета
                COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as tp_count,
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
                COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as trailing_count,

                COUNT(CASE WHEN close_reason = 'timeout' THEN 1 END) as timeout_count,
                COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_count,
                COALESCE(SUM(pnl_usd), 0) as total_pnl,

                -- Прибыль только от TP
                COALESCE(SUM(CASE 
                    WHEN close_reason = 'take_profit' THEN pnl_usd
                    ELSE 0
                END), 0) as tp_profit,

                -- Убытки только от SL
                COALESCE(SUM(CASE 
                    WHEN close_reason = 'stop_loss' THEN ABS(pnl_usd)
                    ELSE 0
                END), 0) as sl_loss,

                -- Отдельная статистика для trailing stops
                COALESCE(SUM(CASE 
                    WHEN close_reason = 'trailing_stop' THEN pnl_usd
                    ELSE 0
                END), 0) as trailing_pnl,
                
                -- Дополнительная статистика trailing
                COUNT(CASE WHEN close_reason = 'trailing_stop' AND pnl_usd > 0 THEN 1 END) as trailing_wins,
                COUNT(CASE WHEN close_reason = 'trailing_stop' AND pnl_usd <= 0 THEN 1 END) as trailing_losses,

                SUM(max_potential_profit_usd) as total_max_potential,
                AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close,
                COUNT(CASE WHEN exchange_name = 'Binance' THEN 1 END) as binance_signals,
                COUNT(CASE WHEN exchange_name = 'Bybit' THEN 1 END) as bybit_signals
            FROM web.scoring_analysis_results
            WHERE session_id = %s AND user_id = %s
        """

    # Безопасно получаем статистику с обработкой ошибок
    try:
        stats_result = db.execute_query(stats_query, (session_id, user_id), fetch=True)
        stats = stats_result[0] if stats_result else {
            'total': 0, 'buy_signals': 0, 'sell_signals': 0,
            'tp_count': 0, 'sl_count': 0, 'trailing_count': 0,
            'timeout_count': 0, 'open_count': 0, 'total_pnl': 0,
            'tp_profit': 0, 'sl_loss': 0, 'trailing_pnl': 0,
            'trailing_wins': 0, 'trailing_losses': 0,
            'total_max_potential': 0, 'avg_hours_to_close': 0,
            'binance_signals': 0, 'bybit_signals': 0
        }
    except Exception as e:
        print(f"[SCORING] Ошибка получения статистики: {e}")
        stats = {
            'total': processed_count, 'buy_signals': 0, 'sell_signals': 0,
            'tp_count': debug_stats.get('closed_tp', 0),
            'sl_count': debug_stats.get('closed_sl', 0),
            'trailing_count': debug_stats.get('closed_trailing', 0),
            'timeout_count': debug_stats.get('closed_timeout', 0),
            'open_count': debug_stats.get('still_open', 0),
            'total_pnl': 0, 'tp_profit': 0, 'sl_loss': 0,
            'trailing_pnl': 0, 'trailing_wins': 0, 'trailing_losses': 0,
            'total_max_potential': 0, 'avg_hours_to_close': 0,
            'binance_signals': 0, 'bybit_signals': 0
        }

    return {
        'processed': processed_count,
        'errors': error_count,
        'stats': stats,
        'exchange_breakdown': exchange_stats
    }


def process_scoring_signals_batch_v2(db, signals, session_id, user_id,
                                     tp_percent=None, sl_percent=None,
                                     position_size=None, leverage=None,
                                     use_trailing_stop=None,
                                     trailing_distance_pct=None,
                                     trailing_activation_pct=None,
                                     max_trades_per_15min=None,
                                     initial_capital=None):
    """
    НОВАЯ ВЕРСИЯ: Wave-based scoring analysis с управлением капиталом

    Отличия от v1:
    - Управление капиталом (capital management)
    - Группировка сигналов по 15-минутным волнам
    - Последовательная обработка волн
    - Отслеживание открытых позиций (position tracking)
    - Проверка дубликатов по паре
    - Приоритизация по score_week
    - Расчет min_equity с floating PnL
    - Дополнительные метрики (max_drawdown, max_concurrent_positions, etc.)
    """
    from trading_simulation import TradingSimulation
    from datetime import timedelta

    # Параметры по умолчанию
    tp_percent = tp_percent if tp_percent is not None else Config.DEFAULT_TAKE_PROFIT_PERCENT
    sl_percent = sl_percent if sl_percent is not None else Config.DEFAULT_STOP_LOSS_PERCENT
    position_size = position_size if position_size is not None else Config.DEFAULT_POSITION_SIZE
    leverage = leverage if leverage is not None else Config.DEFAULT_LEVERAGE
    use_trailing_stop = use_trailing_stop if use_trailing_stop is not None else Config.DEFAULT_USE_TRAILING_STOP
    trailing_distance_pct = trailing_distance_pct if trailing_distance_pct is not None else Config.DEFAULT_TRAILING_DISTANCE_PCT
    trailing_activation_pct = trailing_activation_pct if trailing_activation_pct is not None else Config.DEFAULT_TRAILING_ACTIVATION_PCT
    max_trades_per_15min = max_trades_per_15min if max_trades_per_15min is not None else Config.DEFAULT_MAX_TRADES_PER_15MIN
    initial_capital = initial_capital if initial_capital is not None else Config.INITIAL_CAPITAL

    wave_interval = Config.WAVE_INTERVAL_MINUTES

    # КРИТИЧНО: Вычисляем simulation_end_time В НАЧАЛЕ (как в check_wr_final.py:385)
    if signals:
        last_signal_time = max(s['timestamp'] for s in signals)
        simulation_end_time = last_signal_time + timedelta(hours=24)
    else:
        simulation_end_time = None

    print(f"\n[SCORING V2] ===== WAVE-BASED SCORING ANALYSIS =====")
    print(f"[SCORING V2] Всего сигналов: {len(signals)}")
    print(f"[SCORING V2] Режим: {'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'}")
    print(f"[SCORING V2] Параметры: TP={tp_percent}%, SL={sl_percent}%, Size=${position_size}, Lev={leverage}x")
    print(f"[SCORING V2] Капитал: ${initial_capital}, Wave: {wave_interval}min, Max trades/wave: {max_trades_per_15min}")
    print(f"[SCORING V2] Simulation end time: {simulation_end_time}")
    if use_trailing_stop:
        print(f"[SCORING V2] Trailing: Activation={trailing_activation_pct}%, Distance={trailing_distance_pct}%")

    # Очищаем предыдущие результаты
    clear_query = """
        DELETE FROM web.scoring_analysis_results
        WHERE session_id = %s AND user_id = %s
    """
    db.execute_query(clear_query, (session_id, user_id))

    # Инициализируем симуляцию
    sim = TradingSimulation(
        initial_capital=initial_capital,
        position_size=position_size,
        leverage=leverage,
        tp_percent=tp_percent,
        sl_percent=sl_percent,
        use_trailing_stop=use_trailing_stop,
        trailing_distance_pct=trailing_distance_pct,
        trailing_activation_pct=trailing_activation_pct
    )

    # Группируем сигналы по волнам
    signals_by_wave = group_signals_by_wave(signals, wave_interval)

    print(f"[SCORING V2] Сигналы сгруппированы в {len(signals_by_wave)} волн")

    # Кэш для market_data (чтобы не загружать несколько раз для одной пары)
    market_data_cache = {}

    def get_market_data(trading_pair_id, signal_timestamp):
        """Получает market_data с кэшированием"""
        cache_key = (trading_pair_id, signal_timestamp)
        if cache_key in market_data_cache:
            return market_data_cache[cache_key]

        # Используем helper function для миграции на public.candles
        history_query = build_candle_history_query(duration_hours=24)
        start_ts = convert_timestamp_param(signal_timestamp)

        # Для public.candles: end_ts = start_ts + 24h в миллисекундах
        if Config.USE_PUBLIC_CANDLES:
            end_ts = start_ts + (24 * 60 * 60 * 1000)  # +24 часа в миллисекундах
        else:
            end_ts = signal_timestamp

        history = db.execute_query(
            history_query,
            (trading_pair_id, start_ts, end_ts),
            fetch=True
        )
        market_data_cache[cache_key] = history
        return history

    # Обработка волн последовательно
    batch_data = []
    total_processed = 0

    for wave_idx, wave_time in enumerate(sorted(signals_by_wave.keys()), 1):
        print(f"\n[SCORING V2] === Волна {wave_idx}/{len(signals_by_wave)}: {wave_time} ===")

        # 1. Закрываем позиции, которые должны закрыться до этой волны
        closed_pairs = sim.close_due_positions(wave_time)
        if closed_pairs:
            print(f"[SCORING V2] Закрыто позиций: {len(closed_pairs)} ({', '.join(closed_pairs[:5])}{'...' if len(closed_pairs) > 5 else ''})")

        # 2. Обновляем метрики equity (с учетом floating PnL открытых позиций)
        # Подготавливаем market_data для расчета floating PnL
        market_data_by_pair = {}
        for pair, position in sim.open_positions.items():
            # Получаем market_data из кэша
            signal_id = position.get('signal_id')
            trading_pair_id = position.get('trading_pair_id')
            signal_timestamp = position.get('signal_timestamp')

            # Пробуем получить из кэша
            if signal_id and signal_id in market_data_cache:
                market_data_by_pair[pair] = market_data_cache[signal_id]
            elif trading_pair_id and signal_timestamp:
                # Если нет в кэше, получаем данные
                cache_key = f"{trading_pair_id}_{signal_timestamp}"
                if cache_key in market_data_cache:
                    market_data_by_pair[pair] = market_data_cache[cache_key]
                else:
                    # Загружаем market_data
                    market_data = get_market_data(trading_pair_id, signal_timestamp)
                    if market_data:
                        market_data_cache[cache_key] = market_data
                        market_data_by_pair[pair] = market_data

        # Теперь передаем реальные данные для расчета floating PnL
        sim.update_equity_metrics(wave_time, market_data_by_pair)

        print(f"[EQUITY UPDATE] Positions: {len(sim.open_positions)}, "
              f"Market data: {len(market_data_by_pair)}")

        # 3. Обрабатываем сигналы текущей волны
        wave_candidates = signals_by_wave[wave_time]
        print(f"[SCORING V2] Кандидатов в волне: {len(wave_candidates)} (отсортированы по score_week)")
        print(f"[SCORING V2] Доступный капитал: ${sim.available_capital:.2f} / ${sim.initial_capital:.2f}")
        print(f"[SCORING V2] Открытых позиций: {len(sim.open_positions)}")

        trades_taken_this_wave = 0

        for signal in wave_candidates:
            sim.stats['total_signals_processed'] += 1
            # Лимит на волну
            if trades_taken_this_wave >= max_trades_per_15min:
                sim.stats['skipped_wave_limit'] += len(wave_candidates) - trades_taken_this_wave
                print(f"[SCORING V2] Достигнут лимит сделок на волну ({max_trades_per_15min})")
                break

            pair_symbol = signal.get('pair_symbol')
            trading_pair_id = signal['trading_pair_id']
            signal_timestamp = signal['timestamp']

            # Получаем entry_price (используем helper function, 15-min window)
            entry_price_query = build_entry_price_query(window_minutes=15)
            ts_param = convert_timestamp_param(signal_timestamp)

            price_result = db.execute_query(
                entry_price_query,
                (trading_pair_id, ts_param, ts_param, ts_param),
                fetch=True
            )

            if not price_result:
                sim.stats['skipped_no_capital'] += 1  # Используем этот счетчик для "нет данных"
                continue

            entry_price = float(price_result[0]['entry_price'])

            # Получаем market_data для симуляции
            market_data = get_market_data(trading_pair_id, signal_timestamp)

            if not market_data:
                sim.stats['skipped_no_capital'] += 1
                continue

            # Пытаемся открыть позицию через TradingSimulation (передаем simulation_end_time)
            result = sim.open_position(signal, entry_price, market_data, simulation_end_time=simulation_end_time)

            if result['success']:
                trades_taken_this_wave += 1
                total_processed += 1

                position = result['position']
                sim_result = position['simulation_result']

                # Подготавливаем данные для БД
                batch_data.append((
                    session_id,
                    user_id,
                    signal['timestamp'],
                    pair_symbol,
                    trading_pair_id,
                    signal['signal_action'],
                    signal.get('market_regime', 'NEUTRAL'),
                    signal.get('exchange_name', 'Unknown'),
                    float(signal.get('total_score', 0)),
                    float(signal.get('indicator_score', 0)),
                    float(signal.get('pattern_score', 0)),
                    float(signal.get('combination_score', 0)),
                    float(signal.get('score_week', 0)),
                    float(signal.get('score_month', 0)),
                    entry_price,
                    sim_result.get('best_price', entry_price),  # best_price_reached
                    sim_result.get('close_price'),
                    sim_result.get('close_time'),
                    sim_result.get('is_closed', False),
                    sim_result.get('close_reason'),
                    (sim_result.get('close_time') - signal['timestamp']).total_seconds() / 3600 if sim_result.get('close_time') else 0,
                    sim_result.get('pnl_percent', 0),
                    sim_result.get('pnl_usd', 0),
                    sim_result.get('max_profit_percent', 0),
                    sim_result.get('max_profit_usd', 0),
                    tp_percent,
                    sl_percent,
                    position_size,
                    leverage
                ))

                # Вставляем пачками
                if len(batch_data) >= 50:
                    _insert_batch_results(db, batch_data)
                    batch_data = []
            else:
                reason = result['reason']
                # Причина уже учтена в sim.stats

        print(f"[SCORING V2] Открыто сделок в волне: {trades_taken_this_wave}/{len(wave_candidates)}")

    # Принудительно закрываем все оставшиеся позиции (если есть)
    if simulation_end_time:
        print(f"\n[SCORING V2] Принудительное закрытие оставшихся позиций на {simulation_end_time}")
        sim.force_close_all_positions(simulation_end_time)

    # Вставляем оставшиеся данные
    if batch_data:
        _insert_batch_results(db, batch_data)

    # Получаем итоговую сводку
    summary = sim.get_summary()

    print(f"\n[SCORING V2] ===== ИТОГОВАЯ СТАТИСТИКА =====")
    print(f"[SCORING V2] Обработано сигналов: {sim.stats['total_signals_processed']}")
    print(f"[SCORING V2] Открыто сделок: {sim.stats['trades_opened']}")
    print(f"[SCORING V2] Закрыто сделок: {sim.stats['trades_closed']}")
    print(f"[SCORING V2] Пропущено (нет капитала): {sim.stats['skipped_no_capital']}")
    print(f"[SCORING V2] Пропущено (дубликат пары): {sim.stats['skipped_duplicate']}")
    print(f"[SCORING V2] Пропущено (лимит волны): {sim.stats['skipped_wave_limit']}")
    print(f"\n[SCORING V2] === ФИНАНСОВЫЕ РЕЗУЛЬТАТЫ ===")
    print(f"[SCORING V2] Начальный капитал: ${summary['initial_capital']:.2f}")
    print(f"[SCORING V2] Итоговый equity: ${summary['final_equity']:.2f}")
    print(f"[SCORING V2] Total PnL: ${summary['total_pnl']:.2f} ({summary['total_pnl_percent']:.2f}%)")
    print(f"[SCORING V2] Win Rate: {summary['win_rate']:.2f}% ({summary['wins']}/{summary['total_trades']})")
    print(f"[SCORING V2] Max Concurrent Positions: {summary['max_concurrent_positions']}")
    print(f"[SCORING V2] Min Equity: ${summary['min_equity']:.2f}")
    print(f"[SCORING V2] Max Drawdown: ${summary['max_drawdown_usd']:.2f} ({summary['max_drawdown_percent']:.2f}%)")
    print(f"[SCORING V2] Total Commission Paid: ${summary['total_commission_paid']:.2f}")

    # Сохраняем summary в БД
    save_scoring_session_summary(
        db, session_id, user_id, summary, sim.stats,
        position_size, leverage, tp_percent, sl_percent,
        use_trailing_stop, trailing_distance_pct, trailing_activation_pct
    )

    # Получаем статистику из БД (для совместимости с UI)
    stats_query = """
        SELECT
            COUNT(*) as total,
            COUNT(CASE WHEN signal_action = 'BUY' THEN 1 END) as buy_signals,
            COUNT(CASE WHEN signal_action = 'SELL' THEN 1 END) as sell_signals,
            COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as tp_count,
            COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
            COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as trailing_count,
            COUNT(CASE WHEN close_reason = 'timeout' THEN 1 END) as timeout_count,
            COUNT(CASE WHEN close_reason = 'smart_loss' THEN 1 END) as smart_loss_count,
            COUNT(CASE WHEN close_reason = 'breakeven' THEN 1 END) as breakeven_count,
            COUNT(CASE WHEN close_reason = 'liquidation' THEN 1 END) as liquidation_count,
            COUNT(CASE WHEN close_reason = 'period_end' THEN 1 END) as period_end_count,
            COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_count,
            COALESCE(SUM(pnl_usd), 0) as total_pnl,
            COALESCE(SUM(CASE WHEN close_reason = 'take_profit' THEN pnl_usd ELSE 0 END), 0) as tp_profit,
            COALESCE(SUM(CASE WHEN close_reason = 'stop_loss' THEN ABS(pnl_usd) ELSE 0 END), 0) as sl_loss,
            COALESCE(SUM(CASE WHEN close_reason = 'trailing_stop' THEN pnl_usd ELSE 0 END), 0) as trailing_pnl,
            COUNT(CASE WHEN close_reason = 'trailing_stop' AND pnl_usd > 0 THEN 1 END) as trailing_wins,
            COUNT(CASE WHEN close_reason = 'trailing_stop' AND pnl_usd <= 0 THEN 1 END) as trailing_losses,
            SUM(max_potential_profit_usd) as total_max_potential,
            AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close,
            COUNT(CASE WHEN exchange_name = 'Binance' THEN 1 END) as binance_signals,
            COUNT(CASE WHEN exchange_name = 'Bybit' THEN 1 END) as bybit_signals
        FROM web.scoring_analysis_results
        WHERE session_id = %s AND user_id = %s
    """

    try:
        stats_result = db.execute_query(stats_query, (session_id, user_id), fetch=True)
        db_stats = stats_result[0] if stats_result else {}
    except Exception as e:
        print(f"[SCORING V2] Ошибка получения статистики из БД: {e}")
        db_stats = {
            'total': total_processed,
            'buy_signals': 0,
            'sell_signals': 0,
            'tp_count': 0,
            'sl_count': 0,
            'trailing_count': 0,
            'timeout_count': 0,
            'open_count': 0,
            'total_pnl': summary['total_pnl']
        }

    return {
        'processed': total_processed,
        'errors': 0,
        'simulation_summary': summary,
        'stats': db_stats  # Используем статистику из БД вместо sim.stats
    }


def save_scoring_session_summary(db, session_id, user_id, summary, stats,
                                 position_size, leverage, tp_percent, sl_percent,
                                 use_trailing_stop, trailing_distance_pct, trailing_activation_pct):
    """
    Сохраняет итоговые метрики wave-based scoring в БД

    Args:
        db: Database connection
        session_id: ID сессии
        user_id: ID пользователя
        summary: Словарь с метриками из TradingSimulation.get_summary()
        stats: Статистика обработки из sim.stats
        position_size, leverage, tp_percent, sl_percent: Параметры торговли
        use_trailing_stop, trailing_distance_pct, trailing_activation_pct: Параметры TS
    """
    insert_query = """
        INSERT INTO web.scoring_session_summary (
            session_id, user_id,
            initial_capital, final_equity, min_equity,
            total_pnl, total_pnl_percent,
            total_trades, wins, losses, win_rate,
            max_concurrent_positions,
            max_drawdown_usd, max_drawdown_percent,
            total_commission_paid,
            total_signals_processed, trades_opened, trades_closed,
            skipped_no_capital, skipped_duplicate, skipped_wave_limit,
            position_size, leverage, tp_percent, sl_percent,
            use_trailing_stop, trailing_distance_pct, trailing_activation_pct
        ) VALUES (
            %s, %s,
            %s, %s, %s,
            %s, %s,
            %s, %s, %s, %s,
            %s,
            %s, %s,
            %s,
            %s, %s, %s,
            %s, %s, %s,
            %s, %s, %s, %s,
            %s, %s, %s
        )
    """

    db.execute_query(insert_query, (
        session_id, user_id,
        summary['initial_capital'], summary['final_equity'], summary['min_equity'],
        summary['total_pnl'], summary['total_pnl_percent'],
        summary['total_trades'], summary['wins'], summary['losses'], summary['win_rate'],
        summary['max_concurrent_positions'],
        summary['max_drawdown_usd'], summary['max_drawdown_percent'],
        summary['total_commission_paid'],
        stats['total_signals_processed'], stats['trades_opened'], stats['trades_closed'],
        stats['skipped_no_capital'], stats['skipped_duplicate'], stats['skipped_wave_limit'],
        position_size, leverage, tp_percent, sl_percent,
        use_trailing_stop, trailing_distance_pct, trailing_activation_pct
    ))

    print(f"[SCORING V2] Summary сохранен в БД (session_id={session_id})")


def _insert_batch_results(db, batch_data):
    """
    Вспомогательная функция для batch insert с правильным сохранением exchange_name
    """
    insert_query = """
        INSERT INTO web.scoring_analysis_results (
            session_id, user_id, signal_timestamp, pair_symbol, trading_pair_id,
            signal_action, market_regime, exchange_name,
            total_score, indicator_score, pattern_score, combination_score,
            score_week, score_month,
            entry_price, best_price, close_price, close_time,
            is_closed, close_reason, hours_to_close,
            pnl_percent, pnl_usd,
            max_potential_profit_percent, max_potential_profit_usd,
            tp_percent, sl_percent, position_size, leverage
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """

    for data in batch_data:
        try:
            db.execute_query(insert_query, data, fetch=False)
        except Exception as e:
            print(f"[INSERT] Ошибка вставки записи: {e}")
            # Продолжаем со следующей записью
            continue


def get_scoring_analysis_results(db, session_id, user_id):
    """Получение результатов анализа из БД"""
    query = """
        SELECT * FROM web.scoring_analysis_results
        WHERE session_id = %s AND user_id = %s
        ORDER BY signal_timestamp DESC
    """
    return db.execute_query(query, (session_id, user_id), fetch=True)


def get_scoring_signals_v2(db, date_filter, score_week_min=None, score_month_min=None, 
                          allowed_hours=None, max_trades_per_15min=None):
    """
    Фильтрация сигналов с учетом временных ограничений.
    Использует Config для дефолтных значений
    """
    if max_trades_per_15min is None:
        max_trades_per_15min = Config.DEFAULT_MAX_TRADES_PER_15MIN
    
    # Получение сигналов с фильтром по максимальному количеству сделок за 15 минут
    
    print(f"\n[SCORING V2] ========== ПОЛУЧЕНИЕ СИГНАЛОВ С ФИЛЬТРОМ 15 МИН ==========")
    print(f"[SCORING V2] Дата: {date_filter}")
    print(f"[SCORING V2] Минимальный score_week: {score_week_min}")
    print(f"[SCORING V2] Минимальный score_month: {score_month_min}")
    print(f"[SCORING V2] Максимум сделок за 15 минут: {max_trades_per_15min}")
    if allowed_hours is not None and len(allowed_hours) > 0 and len(allowed_hours) < 24:
        print(f"[SCORING V2] Разрешенные часы (UTC): {sorted(allowed_hours)}")
    
    # Сначала получаем все сигналы С УЧЕТОМ фильтра по часам
    all_signals = get_scoring_signals(db, date_filter, score_week_min, score_month_min, allowed_hours)
    
    if not all_signals:
        print(f"[SCORING V2] Нет сигналов для фильтрации")
        return []
    
    print(f"[SCORING V2] Найдено {len(all_signals)} сигналов после фильтра по часам")
    
    # Проверяем первый сигнал для отладки
    if all_signals:
        first_signal = all_signals[0]
        if isinstance(first_signal, dict):
            print(f"[SCORING V2] Поля первого сигнала: {list(first_signal.keys())}")
        else:
            print(f"[SCORING V2] WARNING: Сигнал не является словарем, тип: {type(first_signal)}")
            print(f"[SCORING V2] Содержимое: {first_signal}")
    
    # Группируем сигналы по 15-минутным интервалам
    from datetime import datetime, timedelta
    
    signals_by_interval = {}
    
    for signal in all_signals:
        # Проверяем, что сигнал является словарем
        if not isinstance(signal, dict):
            print(f"[SCORING V2] WARNING: Сигнал не является словарем, пропускаем")
            continue
            
        # Получаем timestamp и округляем до 15 минут
        timestamp = signal.get('timestamp')
        if not timestamp:
            print(f"[SCORING V2] WARNING: Сигнал без timestamp, пропускаем: {signal.get('signal_id', 'unknown')}")
            continue
        
        # Округляем до 15 минут
        minutes = timestamp.minute
        rounded_minutes = (minutes // 15) * 15
        interval_key = timestamp.replace(minute=rounded_minutes, second=0, microsecond=0)
        
        if interval_key not in signals_by_interval:
            signals_by_interval[interval_key] = []
        signals_by_interval[interval_key].append(signal)
    
    print(f"[SCORING V2] Найдено {len(signals_by_interval)} уникальных 15-минутных интервалов")
    
    # Выбираем топ N сигналов с максимальным score_week из каждого интервала
    filtered_signals = []
    
    for interval, signals in sorted(signals_by_interval.items()):
        # Сортируем сигналы по score_week (убывание)
        sorted_signals = sorted(signals, key=lambda x: x.get('score_week', 0), reverse=True)
        
        # Берем только первые max_trades_per_15min сигналов
        top_signals = sorted_signals[:max_trades_per_15min]
        filtered_signals.extend(top_signals)
        
        if len(signals) > max_trades_per_15min:
            print(f"[SCORING V2] Интервал {interval}: {len(signals)} сигналов -> оставлено {len(top_signals)}")
    
    # Сортируем итоговый список по timestamp
    filtered_signals.sort(key=lambda x: x['timestamp'], reverse=True)
    
    print(f"[SCORING V2] После фильтрации осталось {len(filtered_signals)} сигналов")
    
    # Статистика по биржам
    exchanges_count = {}
    for signal in filtered_signals:
        exchange = signal.get('exchange_name', 'Unknown')
        exchanges_count[exchange] = exchanges_count.get(exchange, 0) + 1
    
    print("\n[SCORING V2] Распределение отфильтрованных сигналов по биржам:")
    for exchange, count in exchanges_count.items():
        print(f"  {exchange}: {count} сигналов")
    
    return filtered_signals


def save_user_scoring_filters(db, user_id, filter_name, buy_filters, sell_filters):
    """Сохранение пользовательских фильтров скоринга"""
    query = """
        INSERT INTO web.user_scoring_filters (
            user_id, filter_name, buy_filters, sell_filters
        ) VALUES (%s, %s, %s, %s)
        ON CONFLICT (user_id, filter_name) DO UPDATE SET
            buy_filters = EXCLUDED.buy_filters,
            sell_filters = EXCLUDED.sell_filters,
            updated_at = NOW()
    """

    import json
    db.execute_query(query, (
        user_id,
        filter_name,
        json.dumps(buy_filters),  # Преобразуем в JSON строку для JSONB
        json.dumps(sell_filters)
    ))


def get_user_scoring_filters(db, user_id):
    """Получение сохраненных фильтров пользователя"""
    query = """
        SELECT filter_name, buy_filters, sell_filters
        FROM web.user_scoring_filters
        WHERE user_id = %s
        ORDER BY updated_at DESC
    """

    results = db.execute_query(query, (user_id,), fetch=True)

    if results:
        filters = []
        for row in results:
            # JSONB автоматически преобразуется в Python объекты psycopg3
            filters.append({
                'name': row['filter_name'],
                'buy_filters': row['buy_filters'] if row['buy_filters'] else [],
                'sell_filters': row['sell_filters'] if row['sell_filters'] else []
            })
        return filters

    return []


def get_best_scoring_signals_with_backtest_params(db, selected_exchanges=None):
    """
    Получение сигналов с оптимальными параметрами из backtest_summary_binance/bybit.
    Автоматически находит лучшие параметры фильтрации на основе результатов бэктестов.

    Args:
        db: Database instance
        selected_exchanges: list[int] - ID бирж для фильтрации (например [1, 2])
                           По умолчанию [1, 2] (Binance, Bybit)

    Логика выбора лучших параметров:
    1. Для каждой выбранной биржи находим summary с max(total_pnl_usd)
    2. Берем записи где total_pnl_usd >= 85% от максимального
    3. Из этих записей выбираем ту, у которой максимальный win_rate
    4. Используем все параметры из этой записи (SL, TS, max_trades)

    Возвращает: (signals, params_by_exchange)
        signals: список сигналов
        params_by_exchange: dict с параметрами для каждой биржи {exchange_id: {...}}
    """
    # Используем дефолтные биржи если не переданы
    if selected_exchanges is None:
        selected_exchanges = [1, 2]  # Binance, Bybit

    # Валидация
    if not selected_exchanges or not isinstance(selected_exchanges, list):
        print(f"[BEST SIGNALS] ОШИБКА: selected_exchanges должен быть непустым списком")
        return [], {}

    print(f"\n[BEST SIGNALS] ========== ПОЛУЧЕНИЕ СИГНАЛОВ С ОПТИМАЛЬНЫМИ ПАРАМЕТРАМИ ==========")
    print(f"[BEST SIGNALS] Выбранные биржи: {selected_exchanges}")
    print(f"[BEST SIGNALS] Период: последние 24 часа")
    print(f"[BEST SIGNALS] Все параметры берутся из оптимального backtest для каждой биржи")

    # SQL запрос для получения сигналов с оптимальными параметрами
    query = """
    -- 1. CTE для поиска ЛУЧШЕГО ID для Binance
    WITH best_binance_id AS (
        WITH FilteredSummaries AS (
            SELECT DISTINCT ON (total_pnl_usd)
                summary_id,
                win_rate,
                total_pnl_usd
            FROM
                web.backtest_summary_binance
            WHERE
                total_pnl_usd >= (
                    SELECT MAX(total_pnl_usd)
                    FROM web.backtest_summary_binance
                ) * 0.95
            ORDER BY
                total_pnl_usd DESC,
                win_rate DESC
        )
        SELECT
            summary_id
        FROM
            FilteredSummaries
        ORDER BY
            win_rate DESC
        LIMIT 1
    ),

    -- 2. CTE для поиска ЛУЧШЕГО ID для Bybit
    best_bybit_id AS (
        WITH FilteredSummaries AS (
            SELECT DISTINCT ON (total_pnl_usd)
                summary_id,
                win_rate,
                total_pnl_usd
            FROM
                web.backtest_summary_bybit
            WHERE
                total_pnl_usd >= (
                    SELECT MAX(total_pnl_usd)
                    FROM web.backtest_summary_bybit
                ) * 0.95
            ORDER BY
                total_pnl_usd DESC,
                win_rate DESC
        )
        SELECT
            summary_id
        FROM
            FilteredSummaries
        ORDER BY
            win_rate DESC
        LIMIT 1
    ),

    -- 3. CTE с ПАРАМЕТРАМИ из лучших ID
    all_best_params AS (
        ( -- Параметры для Binance
            SELECT
                1 AS exchange_id,
                score_week_filter,
                score_month_filter,
                max_trades_filter,
                stop_loss_filter,
                trailing_activation_filter,
                trailing_distance_filter
            FROM web.backtest_summary_binance
            WHERE summary_id = (SELECT summary_id FROM best_binance_id)
        )

        UNION ALL

        ( -- Параметры для Bybit
            SELECT
                2 AS exchange_id,
                score_week_filter,
                score_month_filter,
                max_trades_filter,
                stop_loss_filter,
                trailing_activation_filter,
                trailing_distance_filter
            FROM web.backtest_summary_bybit
            WHERE summary_id = (SELECT summary_id FROM best_bybit_id)
        )
    ),

    -- 4. CTE для market regime
    market_regime_data AS (
        SELECT DISTINCT ON (DATE_TRUNC('hour', timestamp))
            DATE_TRUNC('hour', timestamp) as hour_bucket,
            regime,
            timestamp
        FROM fas_v2.market_regime
        WHERE timeframe = '4h'
            AND timestamp >= NOW() - INTERVAL '24 hours'
        ORDER BY DATE_TRUNC('hour', timestamp), timestamp DESC
    )

    -- 5. Основной запрос к сигналам
    SELECT
        sc.id as signal_id,
        sc.pair_symbol,
        sc.recommended_action as signal_action,
        sc.score_week,
        sc.score_month,
        sc.timestamp,
        sc.created_at,
        sc.trading_pair_id,
        sc.total_score,
        sc.indicator_score,
        sc.pattern_score,
        sc.combination_score,
        tp.exchange_id,
        ex.exchange_name,
        COALESCE(mr.regime, 'NEUTRAL') AS market_regime,

        -- Выводим параметры из CTE для информации
        bp.score_week_filter,
        bp.score_month_filter,
        bp.max_trades_filter,
        bp.stop_loss_filter,
        bp.trailing_activation_filter,
        bp.trailing_distance_filter

    FROM fas_v2.scoring_history AS sc
    JOIN public.trading_pairs AS tp ON sc.trading_pair_id = tp.id
    JOIN public.exchanges AS ex ON ex.id = tp.exchange_id
    JOIN all_best_params AS bp ON tp.exchange_id = bp.exchange_id
    LEFT JOIN LATERAL (
        SELECT regime
        FROM market_regime_data mr
        WHERE mr.hour_bucket <= DATE_TRUNC('hour', sc.timestamp)
        ORDER BY mr.hour_bucket DESC
        LIMIT 1
    ) AS mr ON true

    WHERE
        sc.timestamp >= NOW() - INTERVAL '24 hours'
        AND sc.is_active = true
        AND tp.is_active = true
        AND tp.contract_type_id = 1
        AND tp.exchange_id = ANY(%s)
        AND sc.score_week > bp.score_week_filter
        AND sc.score_month > bp.score_month_filter
        AND EXTRACT(HOUR FROM sc.timestamp) NOT BETWEEN 0 AND 1
    """

    query += " ORDER BY sc.timestamp DESC"

    try:
        results = db.execute_query(query, (selected_exchanges,), fetch=True)

        if results:
            print(f"[BEST SIGNALS] Найдено {len(results)} сигналов после фильтрации по оптимальным параметрам")

            # Собираем параметры для каждой биржи
            params_by_exchange = {}

            for signal in results:
                exchange_id = signal.get('exchange_id')
                if exchange_id not in params_by_exchange:
                    params_by_exchange[exchange_id] = {
                        'exchange_id': exchange_id,
                        'exchange_name': signal.get('exchange_name'),
                        'score_week_filter': signal.get('score_week_filter'),
                        'score_month_filter': signal.get('score_month_filter'),
                        'max_trades_filter': signal.get('max_trades_filter'),
                        'stop_loss_filter': float(signal.get('stop_loss_filter', 0)),
                        'trailing_activation_filter': float(signal.get('trailing_activation_filter', 0)),
                        'trailing_distance_filter': float(signal.get('trailing_distance_filter', 0))
                    }

            # Выводим найденные оптимальные параметры для каждой биржи
            print(f"\n[BEST SIGNALS] Оптимальные параметры для каждой биржи:")
            for exchange_id, params in params_by_exchange.items():
                print(f"\n  {params['exchange_name']}:")
                print(f"    Score Week: {params['score_week_filter']}")
                print(f"    Score Month: {params['score_month_filter']}")
                print(f"    Max Trades/15min: {params['max_trades_filter']}")
                print(f"    Stop Loss: {params['stop_loss_filter']}%")
                print(f"    Trailing Activation: {params['trailing_activation_filter']}%")
                print(f"    Trailing Distance: {params['trailing_distance_filter']}%")

            # Группируем по биржам для статистики
            exchanges_count = {}
            actions_count = {'BUY': 0, 'SELL': 0, 'LONG': 0, 'SHORT': 0, 'NEUTRAL': 0}

            for signal in results:
                exchange = signal.get('exchange_name', 'Unknown')
                exchanges_count[exchange] = exchanges_count.get(exchange, 0) + 1

                action = signal.get('signal_action', 'NEUTRAL')
                if action in actions_count:
                    actions_count[action] += 1

            print("\n[BEST SIGNALS] Распределение по биржам:")
            for exchange, count in exchanges_count.items():
                print(f"  {exchange}: {count} сигналов")

            print("\n[BEST SIGNALS] Распределение по типам сигналов:")
            for action, count in actions_count.items():
                if count > 0:
                    print(f"  {action}: {count}")

            # Применяем фильтр по 15-минутным интервалам ОТДЕЛЬНО для каждой биржи
            signals_by_exchange = {}
            for signal in results:
                exchange_id = signal.get('exchange_id')
                if exchange_id not in signals_by_exchange:
                    signals_by_exchange[exchange_id] = []
                signals_by_exchange[exchange_id].append(signal)

            filtered_signals = []
            for exchange_id, signals in signals_by_exchange.items():
                params = params_by_exchange[exchange_id]
                max_trades = params['max_trades_filter']

                print(f"\n[BEST SIGNALS] Применение фильтра для {params['exchange_name']}: не более {max_trades} сделок за 15 минут")
                filtered = apply_15min_filter(signals, max_trades)
                filtered_signals.extend(filtered)
                print(f"[BEST SIGNALS] {params['exchange_name']}: {len(signals)} → {len(filtered)} сигналов")

            # Сортируем по времени
            filtered_signals.sort(key=lambda x: x.get('timestamp'), reverse=True)
            print(f"\n[BEST SIGNALS] Итого после фильтрации: {len(filtered_signals)} сигналов")

            return filtered_signals, params_by_exchange
        else:
            print("[BEST SIGNALS] Сигналов не найдено")
            return [], {}

    except Exception as e:
        print(f"[BEST SIGNALS] Ошибка при получении сигналов: {str(e)}")
        import traceback
        traceback.print_exc()
        return [], {}


def apply_15min_filter(signals, max_trades_per_15min):
    """
    Применяет фильтр по 15-минутным интервалам.
    Выбирает топ N сигналов с максимальным score_week из каждого 15-минутного интервала.
    """
    from datetime import datetime, timedelta

    signals_by_interval = {}

    for signal in signals:
        if not isinstance(signal, dict):
            continue

        timestamp = signal.get('timestamp')
        if not timestamp:
            continue

        # Округляем до 15 минут
        minutes = timestamp.minute
        rounded_minutes = (minutes // 15) * 15
        interval_key = timestamp.replace(minute=rounded_minutes, second=0, microsecond=0)

        if interval_key not in signals_by_interval:
            signals_by_interval[interval_key] = []
        signals_by_interval[interval_key].append(signal)

    # Выбираем топ N сигналов из каждого интервала
    filtered_signals = []

    for interval, interval_signals in sorted(signals_by_interval.items()):
        sorted_signals = sorted(interval_signals, key=lambda x: x.get('score_week', 0), reverse=True)
        top_signals = sorted_signals[:max_trades_per_15min]
        filtered_signals.extend(top_signals)

    # Сортируем по timestamp
    filtered_signals.sort(key=lambda x: x['timestamp'], reverse=True)

    return filtered_signals


# =============================================================================
# RAW SIGNALS FEATURE - Functions for displaying signals from fas_v2.scoring_history
# =============================================================================

def get_raw_signals(db, filters, page=1, per_page=50):
    """
    Получение списка сырых сигналов из fas_v2.scoring_history с фильтрацией и пагинацией

    Args:
        db: Database instance
        filters: dict with filter parameters
            {
                'time_range': '1h' | '3h' | '6h' | '12h' | '24h' | 'custom',
                'custom_start': datetime (optional),
                'custom_end': datetime (optional),
                'score_week_min': int,
                'score_week_max': int,
                'score_month_min': int,
                'score_month_max': int,
                'actions': ['BUY', 'SELL', 'NEUTRAL', ...],
                'patterns': ['OI_EXPLOSION', ...] (optional),
                'regimes': ['BULL', 'BEAR', 'NEUTRAL'] (optional),
                'exchanges': [1, 2] (optional, Binance=1, Bybit=2)
            }
        page: int - page number (1-indexed)
        per_page: int - items per page

    Returns:
        dict {
            'signals': [...],
            'total': int,
            'page': int,
            'pages': int
        }
    """
    try:
        logger.info(f"get_raw_signals called with filters: {filters}, page: {page}, per_page: {per_page}")

        # DEBUG: write to file
        with open('/tmp/raw_signals_debug.log', 'a') as f:
            import datetime
            f.write(f"\n{datetime.datetime.now()}: get_raw_signals called\n")
            f.write(f"  Filters: {filters}\n")
            f.write(f"  Page: {page}, Per page: {per_page}\n")
            f.flush()

        # Построение WHERE условий
        where_clauses = ["sh.is_active = true"]
        params = []

        # Временной диапазон
        time_range = filters.get('time_range', '24h')
        if time_range == 'custom':
            if filters.get('custom_start'):
                where_clauses.append("sh.timestamp >= %s")
                params.append(filters['custom_start'])
            if filters.get('custom_end'):
                where_clauses.append("sh.timestamp <= %s")
                params.append(filters['custom_end'])
        else:
            # Предустановленные диапазоны
            hours_map = {'1h': 1, '3h': 3, '6h': 6, '12h': 12, '24h': 24}
            hours = hours_map.get(time_range, 24)
            where_clauses.append(f"sh.timestamp >= NOW() - INTERVAL '{hours} hours'")

        # Score Week фильтр
        if filters.get('score_week_min') is not None:
            where_clauses.append("sh.score_week >= %s")
            params.append(filters['score_week_min'])

        if filters.get('score_week_max') is not None:
            where_clauses.append("sh.score_week <= %s")
            params.append(filters['score_week_max'])

        # Score Month фильтр
        if filters.get('score_month_min') is not None:
            where_clauses.append("sh.score_month >= %s")
            params.append(filters['score_month_min'])

        if filters.get('score_month_max') is not None:
            where_clauses.append("sh.score_month <= %s")
            params.append(filters['score_month_max'])

        # Действие фильтр
        if filters.get('actions') and len(filters['actions']) > 0:
            where_clauses.append("sh.recommended_action = ANY(%s)")
            params.append(filters['actions'])

        # Биржа фильтр
        if filters.get('exchanges') and len(filters['exchanges']) > 0:
            where_clauses.append("tp.exchange_id = ANY(%s)")
            params.append(filters['exchanges'])

        # Паттерны фильтр (через EXISTS подзапрос)
        if filters.get('patterns') and len(filters['patterns']) > 0:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM fas_v2.sh_patterns shp
                    JOIN fas_v2.signal_patterns sp ON sp.id = shp.signal_patterns_id
                    WHERE shp.scoring_history_id = sh.id
                    AND sp.pattern_type = ANY(%s)
                )
            """)
            params.append(filters['patterns'])

        # Режим рынка фильтр
        if filters.get('regimes') and len(filters['regimes']) > 0:
            where_clauses.append("mr.regime = ANY(%s)")
            params.append(filters['regimes'])

        where_sql = " AND ".join(where_clauses)

        # Подсчет общего количества (для пагинации)
        count_query = f"""
            SELECT COUNT(DISTINCT sh.id) as total
            FROM fas_v2.scoring_history sh
            JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
            LEFT JOIN fas_v2.sh_regime shr ON shr.scoring_history_id = sh.id
            LEFT JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
            WHERE {where_sql}
        """

        logger.info(f"WHERE clause: {where_sql}")
        logger.info(f"Params: {params}")
        print(f"DEBUG: WHERE clause: {where_sql}", flush=True)
        print(f"DEBUG: Params: {params}", flush=True)

        # DEBUG: Before get_connection
        with open('/tmp/raw_signals_debug.log', 'a') as f:
            f.write(f"  About to get connection...\n")
            f.flush()

        with db.get_connection() as conn:
            # DEBUG: After get_connection
            with open('/tmp/raw_signals_debug.log', 'a') as f:
                f.write(f"  Got connection!\n")
                f.flush()
            with conn.cursor(row_factory=dict_row) as cur:
                logger.info(f"Executing count query...")
                print(f"DEBUG: Executing count query...", flush=True)

                # DEBUG: write SQL to file
                with open('/tmp/raw_signals_debug.log', 'a') as f:
                    f.write(f"  WHERE: {where_sql}\n")
                    f.write(f"  PARAMS: {params}\n")
                    f.flush()

                try:
                    with open('/tmp/raw_signals_debug.log', 'a') as f:
                        f.write(f"  Executing cur.execute...\n")
                        f.flush()

                    cur.execute(count_query, params)

                    with open('/tmp/raw_signals_debug.log', 'a') as f:
                        f.write(f"  Execute done, fetching...\n")
                        f.flush()

                    total = cur.fetchone()['total']

                    with open('/tmp/raw_signals_debug.log', 'a') as f:
                        f.write(f"  TOTAL FOUND: {total}\n")
                        f.flush()

                    logger.info(f"Total signals found: {total}")
                    print(f"DEBUG: Total signals found: {total}", flush=True)
                except Exception as e:
                    with open('/tmp/raw_signals_debug.log', 'a') as f:
                        f.write(f"  ERROR in execute: {e}\n")
                        f.flush()
                    raise

                # Расчет пагинации
                pages = (total + per_page - 1) // per_page if total > 0 else 1
                page = max(1, min(page, pages))
                offset = (page - 1) * per_page

                # Основной запрос с пагинацией
                query = f"""
                    SELECT
                        sh.id,
                        sh.timestamp,
                        sh.pair_symbol,
                        sh.recommended_action,
                        sh.score_week,
                        sh.score_month,
                        sh.total_score,
                        sh.pattern_score,
                        sh.combination_score,
                        sh.indicator_score,
                        sh.patterns_details,
                        sh.combinations_details,
                        tp.exchange_id,
                        ex.exchange_name,
                        mr.regime as market_regime,
                        mr.strength as regime_strength,
                        COUNT(DISTINCT shp.id) as patterns_count,
                        COUNT(DISTINCT shi.id) as indicators_count,
                        CASE
                            WHEN COUNT(DISTINCT shpoc.id) > 0 THEN true
                            ELSE false
                        END as has_poc
                    FROM fas_v2.scoring_history sh
                    JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
                    JOIN exchanges ex ON ex.id = tp.exchange_id
                    LEFT JOIN fas_v2.sh_regime shr ON shr.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
                    LEFT JOIN fas_v2.sh_patterns shp ON shp.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.sh_indicators shi ON shi.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.sh_poc shpoc ON shpoc.scoring_history_id = sh.id
                    WHERE {where_sql}
                    GROUP BY sh.id, tp.exchange_id, ex.exchange_name, mr.regime, mr.strength
                    ORDER BY sh.timestamp DESC
                    LIMIT {per_page} OFFSET {offset}
                """

                logger.info(f"Executing main query for page {page} (offset {offset}, limit {per_page})...")

                # DEBUG
                with open('/tmp/raw_signals_debug.log', 'a') as f:
                    f.write(f"  Executing main query (offset={offset}, limit={per_page})...\n")
                    f.flush()

                cur.execute(query, params)
                signals = cur.fetchall()
                logger.info(f"Fetched {len(signals)} signals")

                # DEBUG
                with open('/tmp/raw_signals_debug.log', 'a') as f:
                    f.write(f"  Fetched {len(signals)} signals\n")
                    if signals:
                        f.write(f"  First signal keys: {list(signals[0].keys())}\n")
                    f.flush()

                return {
                    'signals': signals,
                    'total': total,
                    'page': page,
                    'pages': pages
                }

    except Exception as e:
        logger.error(f"Error getting raw signals: {e}")
        import traceback
        traceback.print_exc()

        # DEBUG
        with open('/tmp/raw_signals_debug.log', 'a') as f:
            f.write(f"  EXCEPTION: {e}\n")
            f.write(f"  Traceback: {traceback.format_exc()}\n")
            f.flush()

        return {
            'signals': [],
            'total': 0,
            'page': 1,
            'pages': 1
        }


def get_signal_details(db, signal_id):
    """
    Получение полной информации о сигнале со всеми связанными данными

    Args:
        db: Database instance
        signal_id: int - ID сигнала из scoring_history

    Returns:
        dict {
            'signal': {...},  # основные данные сигнала
            'patterns': [...],  # список паттернов с деталями
            'indicators': {...},  # индикаторы по таймфреймам
            'poc': {...},  # POC levels (если есть)
            'regime': {...}  # режим рынка (если есть)
        } или None если сигнал не найден
    """
    try:
        with db.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Основная информация о сигнале
                signal_query = """
                    SELECT
                        sh.*,
                        tp.exchange_id,
                        ex.exchange_name
                    FROM fas_v2.scoring_history sh
                    JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
                    JOIN exchanges ex ON ex.id = tp.exchange_id
                    WHERE sh.id = %s
                """
                cur.execute(signal_query, (signal_id,))
                signal = cur.fetchone()

                if not signal:
                    return None

                # Паттерны
                patterns_query = """
                    SELECT
                        sp.id,
                        sp.pattern_type,
                        sp.timeframe,
                        sp.strength,
                        sp.confidence,
                        sp.score_impact,
                        sp.details,
                        sp.trigger_values,
                        sp.pattern_start,
                        sp.pattern_end,
                        sp.duration_minutes
                    FROM fas_v2.sh_patterns shp
                    JOIN fas_v2.signal_patterns sp ON sp.id = shp.signal_patterns_id
                    WHERE shp.scoring_history_id = %s
                    ORDER BY ABS(sp.score_impact) DESC
                """
                cur.execute(patterns_query, (signal_id,))
                patterns = cur.fetchall()

                # Индикаторы (все таймфреймы)
                indicators_query = """
                    SELECT
                        ind.timeframe,
                        ind.close_price,
                        ind.price_change_pct,
                        ind.buy_ratio,
                        ind.buy_ratio_weighted,
                        ind.volume_zscore,
                        ind.normalized_imbalance,
                        ind.smoothed_imbalance,
                        ind.cvd_delta,
                        ind.cvd_cumulative,
                        ind.oi_delta_pct,
                        ind.funding_rate_avg,
                        ind.rsi,
                        ind.atr,
                        ind.macd_line,
                        ind.macd_signal,
                        ind.macd_histogram,
                        ind.rs_value,
                        ind.rs_momentum
                    FROM fas_v2.sh_indicators shi
                    JOIN fas_v2.indicators ind ON (
                        ind.trading_pair_id = shi.indicators_trading_pair_id
                        AND ind.timestamp = shi.indicators_timestamp
                        AND ind.timeframe = shi.indicators_timeframe
                    )
                    WHERE shi.scoring_history_id = %s
                    ORDER BY
                        CASE ind.timeframe::text
                            WHEN '5m' THEN 1
                            WHEN '15m' THEN 2
                            WHEN '1h' THEN 3
                            WHEN '4h' THEN 4
                            WHEN '1d' THEN 5
                            ELSE 99
                        END
                """
                cur.execute(indicators_query, (signal_id,))
                indicators_list = cur.fetchall()

                # Преобразуем список индикаторов в словарь по таймфреймам
                indicators = {}
                for ind in indicators_list:
                    timeframe = str(ind['timeframe'])
                    indicators[timeframe] = ind

                # POC levels
                poc_query = """
                    SELECT
                        poc.poc_24h,
                        poc.poc_7d,
                        poc.poc_30d,
                        poc.volume_24h,
                        poc.volume_7d,
                        poc.data_points_24h,
                        poc.data_points_7d,
                        poc.data_points_30d,
                        poc.calculation_quality,
                        poc.calculated_at
                    FROM fas_v2.sh_poc shpoc
                    JOIN fas_v2.poc_levels poc ON (
                        poc.trading_pair_id = shpoc.poc_trading_pair_id
                        AND poc.calculated_at = shpoc.poc_calculated_at
                    )
                    WHERE shpoc.scoring_history_id = %s
                    LIMIT 1
                """
                cur.execute(poc_query, (signal_id,))
                poc = cur.fetchone()

                # Режим рынка
                regime_query = """
                    SELECT
                        mr.regime,
                        mr.strength,
                        mr.btc_change_1h,
                        mr.btc_change_4h,
                        mr.btc_change_24h,
                        mr.alt_change_1h,
                        mr.alt_change_4h,
                        mr.alt_change_24h,
                        mr.volume_factor,
                        mr.timestamp as regime_timestamp
                    FROM fas_v2.sh_regime shr
                    JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
                    WHERE shr.scoring_history_id = %s
                    LIMIT 1
                """
                cur.execute(regime_query, (signal_id,))
                regime = cur.fetchone()

                return {
                    'signal': signal,
                    'patterns': patterns,
                    'indicators': indicators,
                    'poc': poc,
                    'regime': regime
                }

    except Exception as e:
        logger.error(f"Error getting signal details for ID {signal_id}: {e}")
        import traceback
        traceback.print_exc()
        return None


def get_raw_signals_stats(db, filters):
    """
    Получение статистики по сырым сигналам с учетом фильтров

    Args:
        db: Database instance
        filters: dict - те же фильтры что и в get_raw_signals

    Returns:
        dict {
            'total': int,
            'by_action': {'BUY': 123, 'SELL': 456, ...},
            'by_regime': {'BULL': 234, 'BEAR': 345, 'NEUTRAL': 123},
            'avg_score_week': float,
            'avg_score_month': float,
            'last_signal_time': datetime,
            'pattern_distribution': {'OI_EXPLOSION': 45, ...}
        }
    """
    try:
        # Строим те же WHERE условия что и в get_raw_signals
        where_clauses = ["sh.is_active = true"]
        params = []

        # Временной диапазон
        time_range = filters.get('time_range', '24h')
        if time_range == 'custom':
            if filters.get('custom_start'):
                where_clauses.append("sh.timestamp >= %s")
                params.append(filters['custom_start'])
            if filters.get('custom_end'):
                where_clauses.append("sh.timestamp <= %s")
                params.append(filters['custom_end'])
        else:
            hours_map = {'1h': 1, '3h': 3, '6h': 6, '12h': 12, '24h': 24}
            hours = hours_map.get(time_range, 24)
            where_clauses.append(f"sh.timestamp >= NOW() - INTERVAL '{hours} hours'")

        # Score Week фильтр
        if filters.get('score_week_min') is not None:
            where_clauses.append("sh.score_week >= %s")
            params.append(filters['score_week_min'])

        if filters.get('score_week_max') is not None:
            where_clauses.append("sh.score_week <= %s")
            params.append(filters['score_week_max'])

        # Score Month фильтр
        if filters.get('score_month_min') is not None:
            where_clauses.append("sh.score_month >= %s")
            params.append(filters['score_month_min'])

        if filters.get('score_month_max') is not None:
            where_clauses.append("sh.score_month <= %s")
            params.append(filters['score_month_max'])

        # Действие фильтр
        if filters.get('actions') and len(filters['actions']) > 0:
            where_clauses.append("sh.recommended_action = ANY(%s)")
            params.append(filters['actions'])

        # Биржа фильтр
        if filters.get('exchanges') and len(filters['exchanges']) > 0:
            where_clauses.append("tp.exchange_id = ANY(%s)")
            params.append(filters['exchanges'])

        # Паттерны фильтр
        if filters.get('patterns') and len(filters['patterns']) > 0:
            where_clauses.append("""
                EXISTS (
                    SELECT 1 FROM fas_v2.sh_patterns shp
                    JOIN fas_v2.signal_patterns sp ON sp.id = shp.signal_patterns_id
                    WHERE shp.scoring_history_id = sh.id
                    AND sp.pattern_type = ANY(%s)
                )
            """)
            params.append(filters['patterns'])

        # Режим рынка фильтр
        if filters.get('regimes') and len(filters['regimes']) > 0:
            where_clauses.append("mr.regime = ANY(%s)")
            params.append(filters['regimes'])

        where_sql = " AND ".join(where_clauses)

        with db.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                # Основная статистика
                main_stats_query = f"""
                    SELECT
                        COUNT(DISTINCT sh.id) as total,
                        AVG(sh.score_week) as avg_score_week,
                        AVG(sh.score_month) as avg_score_month,
                        MAX(sh.timestamp) as last_signal_time
                    FROM fas_v2.scoring_history sh
                    JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
                    LEFT JOIN fas_v2.sh_regime shr ON shr.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
                    WHERE {where_sql}
                """
                cur.execute(main_stats_query, params)
                main_stats = cur.fetchone()

                # Статистика по действиям
                action_stats_query = f"""
                    SELECT
                        sh.recommended_action,
                        COUNT(DISTINCT sh.id) as count
                    FROM fas_v2.scoring_history sh
                    JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
                    LEFT JOIN fas_v2.sh_regime shr ON shr.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
                    WHERE {where_sql}
                    GROUP BY sh.recommended_action
                """
                cur.execute(action_stats_query, params)
                action_stats_list = cur.fetchall()
                by_action = {row['recommended_action']: row['count'] for row in action_stats_list}

                # Статистика по режимам рынка
                regime_stats_query = f"""
                    SELECT
                        COALESCE(mr.regime, 'UNKNOWN') as regime,
                        COUNT(DISTINCT sh.id) as count
                    FROM fas_v2.scoring_history sh
                    JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
                    LEFT JOIN fas_v2.sh_regime shr ON shr.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
                    WHERE {where_sql}
                    GROUP BY mr.regime
                """
                cur.execute(regime_stats_query, params)
                regime_stats_list = cur.fetchall()
                by_regime = {row['regime']: row['count'] for row in regime_stats_list}

                # Распределение паттернов
                pattern_dist_query = f"""
                    SELECT
                        sp.pattern_type,
                        COUNT(*) as count
                    FROM fas_v2.scoring_history sh
                    JOIN trading_pairs tp ON tp.id = sh.trading_pair_id
                    LEFT JOIN fas_v2.sh_regime shr ON shr.scoring_history_id = sh.id
                    LEFT JOIN fas_v2.market_regime mr ON mr.id = shr.signal_regime_id
                    JOIN fas_v2.sh_patterns shp ON shp.scoring_history_id = sh.id
                    JOIN fas_v2.signal_patterns sp ON sp.id = shp.signal_patterns_id
                    WHERE {where_sql}
                    GROUP BY sp.pattern_type
                    ORDER BY count DESC
                """
                cur.execute(pattern_dist_query, params)
                pattern_dist_list = cur.fetchall()
                pattern_distribution = {row['pattern_type']: row['count'] for row in pattern_dist_list}

                return {
                    'total': main_stats['total'],
                    'by_action': by_action,
                    'by_regime': by_regime,
                    'avg_score_week': float(main_stats['avg_score_week']) if main_stats['avg_score_week'] else 0.0,
                    'avg_score_month': float(main_stats['avg_score_month']) if main_stats['avg_score_month'] else 0.0,
                    'last_signal_time': main_stats['last_signal_time'],
                    'pattern_distribution': pattern_distribution
                }

    except Exception as e:
        logger.error(f"Error getting raw signals stats: {e}")
        import traceback
        traceback.print_exc()
        return {
            'total': 0,
            'by_action': {},
            'by_regime': {},
            'avg_score_week': 0.0,
            'avg_score_month': 0.0,
            'last_signal_time': None,
            'pattern_distribution': {}
        }


# =============================================================================
# EXCHANGE FILTER SUPPORT - Helper functions for exchange filtering
# =============================================================================

def validate_exchange_ids(db, exchange_ids):
    """
    Проверяет что все переданные ID бирж существуют в public.exchanges

    Args:
        db: Database instance
        exchange_ids: list[int] - список ID бирж для проверки

    Returns:
        tuple: (is_valid: bool, valid_ids: list, invalid_ids: list)

    Example:
        >>> is_valid, valid, invalid = validate_exchange_ids(db, [1, 2])
        >>> print(f"Valid: {valid}, Invalid: {invalid}")
        Valid: [1, 2], Invalid: []
    """
    if not exchange_ids:
        return False, [], []

    try:
        query = "SELECT id FROM public.exchanges WHERE id = ANY(%s)"
        results = db.execute_query(query, (exchange_ids,), fetch=True)

        valid_ids = [r['id'] for r in results] if results else []
        invalid_ids = [eid for eid in exchange_ids if eid not in valid_ids]

        is_valid = len(invalid_ids) == 0

        if not is_valid:
            logger.warning(f"[VALIDATE] Invalid exchange IDs: {invalid_ids}")

        return is_valid, valid_ids, invalid_ids

    except Exception as e:
        logger.error(f"[VALIDATE] Ошибка валидации exchange_ids: {e}")
        return False, [], exchange_ids
