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
import pytz
from config import Config


def make_aware(dt):
    """Преобразует naive datetime в aware (UTC)"""
    if dt is None:
        return None
    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        # Если naive, добавляем UTC timezone
        return dt.replace(tzinfo=timezone.utc)
    return dt


def make_naive(dt):
    """Преобразует aware datetime в naive (убирает timezone)"""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        # Конвертируем в UTC и убираем timezone
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


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
            with conn.cursor() as cur:
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
            with conn.cursor() as cur:
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
                with conn.cursor() as cur:
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


def get_recent_large_trades(db, limit=100, time_filter=None, min_value_usd=None, operation_type=None):
    """Получение последних крупных сделок"""
    query = """
        SELECT 
            base_asset,
            quote_asset,
            price,
            quantity,
            value_usd,
            is_sell,
            created_at,
            exchange_id
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

    query += " ORDER BY created_at DESC LIMIT %s"
    params.append(limit)

    return db.execute_query(query, tuple(params), fetch=True)


def initialize_signals_with_params(db, hours_back=48, tp_percent=4.0, sl_percent=3.0):
    """
    Полная инициализация системы с использованием fas.market_data_aggregated
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
            JOIN fas.scoring_history sh ON sh.id = pr.signal_id
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
                            trailing_activation_pct=None):
    """
    Обработка сигнала с поддержкой Trailing Stop
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
    try:
        signal_id = signal['signal_id']
        trading_pair_id = signal['trading_pair_id']
        pair_symbol = signal['pair_symbol']
        score_week = signal.get('score_week', 0)
        score_month = signal.get('score_month', 0)
        signal_action = signal['signal_action']
        signal_timestamp = signal['signal_timestamp']
        exchange_name = signal.get('exchange_name', 'Unknown')
        
        # Проверяем наличие открытых позиций по этой паре
        open_position = has_open_position(db, pair_symbol)
        if open_position:
            logger.warning(f"[DUPLICATE SKIPPED] Пропускаем сигнал {signal_id} для {pair_symbol} - уже есть открытая позиция (signal_id={open_position['signal_id']})")
            return {'success': False, 'reason': 'duplicate_position', 'existing_signal_id': open_position['signal_id']}

        # Инициализируем last_price значением по умолчанию
        last_price = None

        # Получаем цену входа
        entry_price_query = """
            SELECT open_price
            FROM fas.market_data_aggregated
            WHERE trading_pair_id = %s 
                AND timeframe = '15m'
                AND timestamp >= %s - INTERVAL '15 minutes'
                AND timestamp <= %s + INTERVAL '15 minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
            LIMIT 1
        """

        price_result = db.execute_query(
            entry_price_query,
            (trading_pair_id, signal_timestamp, signal_timestamp, signal_timestamp),
            fetch=True
        )

        if not price_result:
            # Расширенный поиск
            fallback_query = """
                SELECT open_price
                FROM fas.market_data_aggregated
                WHERE trading_pair_id = %s
                    AND timeframe = '15m'
                    AND timestamp >= %s - INTERVAL '1 hour'
                    AND timestamp <= %s + INTERVAL '1 hour'
                ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
                LIMIT 1
            """
            price_result = db.execute_query(
                fallback_query,
                (trading_pair_id, signal_timestamp, signal_timestamp, signal_timestamp),
                fetch=True
            )

        if not price_result:
            print(f"[PROCESS] Нет цены для {pair_symbol} ({exchange_name})")
            return {'success': False}

        entry_price = float(price_result[0]['open_price'])

        # Устанавливаем last_price = entry_price по умолчанию
        last_price = entry_price

        # Получаем историю
        history_query = """
            SELECT timestamp, open_price, high_price, low_price, close_price
            FROM fas.market_data_aggregated
            WHERE trading_pair_id = %s
                AND timeframe = '15m'
                AND timestamp >= %s
                AND timestamp <= NOW()
            ORDER BY timestamp ASC
        """

        history = db.execute_query(history_query, (trading_pair_id, signal_timestamp), fetch=True)

        if not history:
            # Сохраняем с начальными данными (с обработкой дубликатов)
            insert_query = """
                INSERT INTO web.web_signals (
                    signal_id, pair_symbol, signal_action, signal_timestamp,
                    entry_price, position_size_usd, leverage,
                    trailing_stop_percent, take_profit_percent,
                    is_closed, last_known_price, use_trailing_stop,
                    score_week, score_month
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s, %s, %s
                )
                ON CONFLICT (signal_id) DO UPDATE SET
                    last_updated_at = NOW()
            """
            db.execute_query(insert_query, (
                signal_id, pair_symbol, signal_action, signal_timestamp,
                entry_price, position_size, leverage,
                trailing_distance_pct if use_trailing_stop else sl_percent,
                tp_percent, entry_price, use_trailing_stop,
                score_week, score_month
            ))
            return {'success': True, 'is_closed': False, 'close_reason': None, 'max_profit': 0}

        # Обновляем last_price из истории если есть данные
        if history:
            last_price = float(history[-1]['close_price'])

        # ВЫБОР ЛОГИКИ: Trailing Stop или Fixed TP/SL
        if use_trailing_stop:
            # Используем новую функцию trailing stop
            result = calculate_trailing_stop_exit(
                entry_price, history, signal_action,
                trailing_distance_pct, trailing_activation_pct,
                sl_percent, position_size, leverage,
                signal_timestamp  # Передаем timestamp для корректного расчета таймаута
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
            # СУЩЕСТВУЮЩАЯ ЛОГИКА Fixed TP/SL (оставляем как есть)
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
                        potential_max_profit = position_size * (max_profit_percent / 100) * leverage
                        if potential_max_profit > max_profit:
                            max_profit = potential_max_profit
                            max_profit_price = best_price_ever

                    # Проверка закрытия для SHORT
                    if not is_closed:
                        if low_price <= tp_price:
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
                            max_profit_price = best_price_ever

                    # Проверка закрытия для LONG
                    if not is_closed:
                        if high_price >= tp_price:
                            is_closed = True
                            close_reason = 'take_profit'
                            close_price = tp_price
                            close_time = current_time
                        elif low_price <= sl_price:
                            is_closed = True
                            close_reason = 'stop_loss'
                            close_price = sl_price
                            close_time = current_time

            # Если не закрылась, проверяем таймаут (48 часов)
            if not is_closed:
                hours_passed = (history[-1]['timestamp'] - signal_timestamp).total_seconds() / 3600
                if hours_passed >= 48:
                    is_closed = True
                    close_reason = 'timeout'
                    close_price = last_price
                    close_time = history[-1]['timestamp']

            # Рассчитываем realized PnL если закрыта
            if is_closed:
                if signal_action in ['SELL', 'SHORT']:
                    pnl_percent = ((entry_price - close_price) / entry_price) * 100
                else:
                    pnl_percent = ((close_price - entry_price) / entry_price) * 100
                realized_pnl = position_size * (pnl_percent / 100) * leverage

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
                score_week, score_month
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
            score_week, score_month
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
                                 signal_timestamp=None):  # ДОБАВЛЯЕМ параметр
    """
    Расчет выхода по trailing stop с ПРАВИЛЬНЫМ отслеживанием максимального профита

    ВАЖНО: Добавлен параметр signal_timestamp для корректного расчета таймаута
    """

    # Переменные для отслеживания trailing stop
    is_trailing_active = False
    trailing_stop_price = None
    best_price_for_trailing = entry_price
    activation_candle_time = None  # Время свечи, на которой активировался trailing

    # ВАЖНО: Отдельная переменная для АБСОЛЮТНОГО максимума за весь период
    absolute_best_price = entry_price
    max_profit_usd = 0

    # Переменные для закрытия позиции
    is_closed = False
    close_price = None
    close_time = None
    close_reason = None

    # Расчет уровней
    if signal_action in ['SELL', 'SHORT']:
        activation_price = entry_price * (1 - trailing_activation_pct / 100)
        insurance_sl_price = entry_price * (1 + sl_percent / 100)
    else:  # BUY, LONG
        activation_price = entry_price * (1 + trailing_activation_pct / 100)
        insurance_sl_price = entry_price * (1 - sl_percent / 100)

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
        current_time = candle['timestamp']
        high_price = float(candle['high_price'])
        low_price = float(candle['low_price'])

        # ============ БЛОК 1: ВСЕГДА обновляем абсолютный максимум ============
        if signal_action in ['SELL', 'SHORT']:
            if low_price < absolute_best_price:
                absolute_best_price = low_price
                max_profit_percent = ((entry_price - absolute_best_price) / entry_price) * 100
                max_profit_usd = position_size * (max_profit_percent / 100) * leverage
        else:  # BUY, LONG
            if high_price > absolute_best_price:
                absolute_best_price = high_price
                max_profit_percent = ((absolute_best_price - entry_price) / entry_price) * 100
                max_profit_usd = position_size * (max_profit_percent / 100) * leverage

        # ============ БЛОК 2: Управление позицией (только если еще открыта) ============
        if not is_closed:
            if signal_action in ['SELL', 'SHORT']:
                # Проверка страховочного SL (только если trailing не активен)
                if not is_trailing_active and high_price >= insurance_sl_price:
                    is_closed = True
                    close_reason = 'stop_loss'
                    close_price = insurance_sl_price
                    close_time = current_time
                    continue  # Переходим к следующей свече для продолжения отслеживания max_profit

                # СНАЧАЛА обновляем best_price
                if low_price < best_price_for_trailing:
                    best_price_for_trailing = low_price

                # ЗАТЕМ проверяем активацию trailing stop
                if not is_trailing_active and best_price_for_trailing <= activation_price:
                    is_trailing_active = True
                    result['trailing_activated'] = True
                    activation_candle_time = current_time  # Запоминаем время активации
                    trailing_stop_price = best_price_for_trailing * (1 + trailing_distance_pct / 100)
                    print(
                        f"[TRAILING] SHORT активирован на {best_price_for_trailing:.8f}, stop={trailing_stop_price:.8f}")
                
                # Если trailing активен, обновляем стоп при изменении best_price
                if is_trailing_active:
                    new_stop = best_price_for_trailing * (1 + trailing_distance_pct / 100)
                    if new_stop < trailing_stop_price:
                        trailing_stop_price = new_stop
                        print(f"[TRAILING] SHORT стоп обновлен до {trailing_stop_price:.8f}")

                # ПОСЛЕ обновления проверяем срабатывание trailing stop
                # НЕ проверяем на свече активации
                if is_trailing_active and current_time != activation_candle_time and high_price >= trailing_stop_price:
                    is_closed = True
                    close_reason = 'trailing_stop'
                    close_price = trailing_stop_price
                    close_time = current_time
                    print(f"[TRAILING] SHORT закрыт по trailing stop на {close_price:.8f}")

            else:  # BUY, LONG
                # Проверка страховочного SL (только если trailing не активен)
                if not is_trailing_active and low_price <= insurance_sl_price:
                    is_closed = True
                    close_reason = 'stop_loss'
                    close_price = insurance_sl_price
                    close_time = current_time
                    continue

                # СНАЧАЛА обновляем best_price
                if high_price > best_price_for_trailing:
                    best_price_for_trailing = high_price

                # ЗАТЕМ проверяем активацию trailing stop
                if not is_trailing_active and best_price_for_trailing >= activation_price:
                    is_trailing_active = True
                    result['trailing_activated'] = True
                    activation_candle_time = current_time  # Запоминаем время активации
                    trailing_stop_price = best_price_for_trailing * (1 - trailing_distance_pct / 100)
                    print(
                        f"[TRAILING] LONG активирован на {best_price_for_trailing:.8f}, stop={trailing_stop_price:.8f}")
                
                # Если trailing активен, обновляем стоп при изменении best_price
                if is_trailing_active:
                    new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
                    if new_stop > trailing_stop_price:
                        trailing_stop_price = new_stop
                        print(f"[TRAILING] LONG стоп обновлен до {trailing_stop_price:.8f}")

                # ПОСЛЕ обновления проверяем срабатывание trailing stop
                # НЕ проверяем на свече активации
                if is_trailing_active and current_time != activation_candle_time and low_price <= trailing_stop_price:
                    is_closed = True
                    close_reason = 'trailing_stop'
                    close_price = trailing_stop_price
                    close_time = current_time
                    print(f"[TRAILING] LONG закрыт по trailing stop на {close_price:.8f}")

    # После обработки ВСЕЙ истории проверяем таймаут
    if not is_closed and len(history) > 0:
        # ИСПРАВЛЕНИЕ: Используем signal_timestamp если он передан
        if signal_timestamp:
            # Считаем от времени сигнала
            hours_passed = (history[-1]['timestamp'] - signal_timestamp).total_seconds() / 3600
        else:
            # Fallback - считаем весь период истории
            hours_passed = (history[-1]['timestamp'] - history[0]['timestamp']).total_seconds() / 3600

        if hours_passed >= 48:
            last_price = float(history[-1]['close_price'])
            is_closed = True
            close_reason = 'timeout'
            close_price = last_price
            close_time = history[-1]['timestamp']
            print(f"[TRAILING] Позиция закрыта по таймауту после {hours_passed:.1f} часов")

    # Формируем результат
    if is_closed:
        if signal_action in ['SELL', 'SHORT']:
            result['pnl_percent'] = ((entry_price - close_price) / entry_price) * 100
        else:
            result['pnl_percent'] = ((close_price - entry_price) / entry_price) * 100

        result['pnl_usd'] = position_size * (result['pnl_percent'] / 100) * leverage
        result['is_closed'] = True
        result['close_reason'] = close_reason
        result['close_price'] = close_price
        result['close_time'] = close_time

    # КРИТИЧНО: Сохраняем максимальный профит независимо от закрытия
    result['max_profit_usd'] = max_profit_usd
    result['best_price'] = best_price_for_trailing
    result['absolute_best_price'] = absolute_best_price

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

        # Получаем цену входа
        entry_price_query = """
            SELECT open_price
            FROM fas.market_data_aggregated
            WHERE trading_pair_id = %s 
                AND timeframe = '15m'
                AND timestamp >= %s - INTERVAL '15 minutes'
                AND timestamp <= %s + INTERVAL '15 minutes'
            ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
            LIMIT 1
        """

        price_result = db.execute_query(
            entry_price_query,
            (trading_pair_id, signal_timestamp, signal_timestamp, signal_timestamp),
            fetch=True
        )

        if not price_result:
            # Расширенный поиск
            fallback_query = """
                SELECT open_price
                FROM fas.market_data_aggregated
                WHERE trading_pair_id = %s
                    AND timeframe = '15m'
                    AND timestamp >= %s - INTERVAL '1 hour'
                    AND timestamp <= %s + INTERVAL '1 hour'
                ORDER BY ABS(EXTRACT(EPOCH FROM (timestamp - %s))) ASC
                LIMIT 1
            """
            price_result = db.execute_query(
                fallback_query,
                (trading_pair_id, signal_timestamp, signal_timestamp, signal_timestamp),
                fetch=True
            )

        if not price_result:
            return {'success': False, 'error': 'No price data'}

        entry_price = float(price_result[0]['open_price'])

        # Получаем историю
        history_query = """
            SELECT timestamp, open_price, high_price, low_price, close_price
            FROM fas.market_data_aggregated
            WHERE trading_pair_id = %s
                AND timeframe = '15m'
                AND timestamp >= %s
                AND timestamp <= NOW()
            ORDER BY timestamp ASC
        """

        history = db.execute_query(history_query, (trading_pair_id, signal_timestamp), fetch=True)

        if not history:
            return {'success': False, 'error': 'No history data'}

        # ВЫБОР ЛОГИКИ: Trailing Stop или Fixed TP/SL
        if use_trailing:
            # Используем trailing stop
            result = calculate_trailing_stop_exit(
                entry_price, history, signal_action,
                trailing_distance, trailing_activation,
                sl_percent, position_size, leverage
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
                        if low_price <= tp_price:
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
                        if high_price >= tp_price:
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

        # Если не закрылась, проверяем таймаут (48 часов)
        if not is_closed:
            hours_passed = (history[-1]['timestamp'] - signal_timestamp).total_seconds() / 3600
            if hours_passed >= 48:
                is_closed = True
                close_reason = 'timeout'
                close_price = last_price
                close_time = history[-1]['timestamp']

        # Рассчитываем финальный PnL
        if is_closed:
            if signal_action in ['SELL', 'SHORT']:
                pnl_percent = ((entry_price - close_price) / entry_price) * 100
            else:
                pnl_percent = ((close_price - entry_price) / entry_price) * 100
            realized_pnl = position_size * (pnl_percent / 100) * leverage
        else:
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
        insert_query = """
            INSERT INTO web.web_signals (
                signal_id, pair_symbol, signal_action, signal_timestamp,
                entry_price, position_size_usd, leverage,
                trailing_stop_percent, take_profit_percent,
                is_closed, closing_price, closed_at, close_reason,
                realized_pnl_usd, unrealized_pnl_usd,
                max_potential_profit_usd, last_known_price,
                score_week, score_month
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
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
            signal.get('score_week', 0), signal.get('score_month', 0)
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
            JOIN fas.scoring_history sh ON sh.id = pr.signal_id
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
        FROM fas.scoring_history
    """
    result = db.execute_query(query, fetch=True)
    return result[0] if result else {'min_date': None, 'max_date': None}


def get_scoring_signals(db, date_filter, score_week_min=None, score_month_min=None, allowed_hours=None):
    """
    Получение сигналов на основе простых фильтров score_week, score_month и allowed_hours
    НОВАЯ ЛОГИКА: Прямой запрос к fas.scoring_history без сложных конструкторов
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

    # Базовый запрос к fas.scoring_history
    query = """
        WITH market_regime_data AS (
            -- Получаем режимы рынка для выбранной даты
            SELECT DISTINCT ON (DATE_TRUNC('hour', timestamp))
                DATE_TRUNC('hour', timestamp) as hour_bucket,
                regime,
                timestamp
            FROM fas.market_regime
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
        FROM fas.scoring_history sh
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
            FROM fas.market_regime
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
            FROM fas.scoring_history sh
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

            price_result = db.execute_query(
                entry_price_query,
                (signal['trading_pair_id'], signal['timestamp'],
                 signal['timestamp'], signal['timestamp']),
                fetch=True
            )

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
                price_result = db.execute_query(
                    extended_query,
                    (signal['trading_pair_id'], signal['timestamp'],
                     signal['timestamp'], signal['timestamp']),
                    fetch=True
                )

                if not price_result:
                    error_count += 1
                    continue

            entry_price = float(price_result[0]['entry_price'])

            # Получаем историю
            history_query = """
                SELECT 
                    timestamp,
                    open_price,
                    high_price,
                    low_price,
                    close_price
                FROM fas.market_data_aggregated
                WHERE trading_pair_id = %s
                    AND timeframe = '15m'
                    AND timestamp >= %s
                    AND timestamp <= %s + INTERVAL '48 hours'
                ORDER BY timestamp ASC
            """

            history = db.execute_query(
                history_query,
                (signal['trading_pair_id'], signal['timestamp'], signal['timestamp']),
                fetch=True
            )

            if not history or len(history) < 2:
                error_count += 1
                continue

            debug_stats['total'] += 1

            # ВЫБОР ЛОГИКИ: Trailing Stop или Fixed TP/SL
            if use_trailing_stop:
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
                    signal_timestamp=signal['timestamp']
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
                # СУЩЕСТВУЮЩАЯ ЛОГИКА Fixed TP/SL
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
                            temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                            if temp_profit_usd > max_profit_usd:
                                max_profit_percent = temp_profit_percent
                                max_profit_usd = temp_profit_usd

                        # Проверка TP/SL для SHORT
                        if not is_closed:
                            if low_price <= tp_price:
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
                            temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                            if temp_profit_usd > max_profit_usd:
                                max_profit_percent = temp_profit_percent
                                max_profit_usd = temp_profit_usd

                        # Проверка TP/SL для LONG
                        if not is_closed:
                            if high_price >= tp_price:
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

                # Рассчитываем финальный P&L
                if signal['signal_action'] in ['SELL', 'SHORT']:
                    final_pnl_percent = ((entry_price - close_price) / entry_price) * 100
                else:
                    final_pnl_percent = ((close_price - entry_price) / entry_price) * 100
                final_pnl_usd = position_size * (final_pnl_percent / 100) * leverage

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
