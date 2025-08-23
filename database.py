"""
Модуль для работы с базой данных PostgreSQL
Работает с существующей таблицей large_trades
Использует psycopg3
"""
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
import os
from contextlib import contextmanager
import logging
from typing import Optional

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Database:
    """Класс для управления подключениями к базе данных"""

    def __init__(self, host=None, port=None, database=None, user=None, password=None, database_url=None, use_pool=True):
        """
        Инициализация базы данных. Можно передать либо отдельные параметры, либо database_url.
        
        Args:
            host: хост базы данных
            port: порт базы данных
            database: имя базы данных
            user: имя пользователя
            password: пароль
            database_url: полная строка подключения (используется если не переданы отдельные параметры)
            use_pool: использовать пул подключений (по умолчанию True)
        """
        if database_url:
            self.database_url = database_url
        else:
            # Формируем строку подключения из отдельных параметров в формате key=value
            self.database_url = f"host={host} port={port} dbname={database} user={user} password={password}"

        self.use_pool = use_pool
        self.connection_pool = None
        if use_pool:
            self._initialize_pool()

    def _initialize_pool(self):
        """Инициализация пула подключений"""
        try:
            self.connection_pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=1,
                max_size=5,  # Уменьшаем максимальное количество подключений
                timeout=10.0,  # Таймаут для получения подключения
                # Важно! Настраиваем пул так, чтобы он не откатывал транзакции автоматически
                reset=False  # Отключаем автоматический сброс соединения
            )
            logger.info("Пул подключений к базе данных инициализирован")
        except Exception as e:
            logger.error(f"Ошибка при инициализации пула подключений: {e}")
            raise

    @contextmanager
    def get_connection(self):
        """Контекстный менеджер для получения подключения"""
        connection = None
        try:
            if self.use_pool:
                connection = self.connection_pool.getconn()
                # Устанавливаем autocommit для правильной работы транзакций
                connection.autocommit = False
            else:
                # Создаем новое подключение без пула
                connection = psycopg.connect(self.database_url, autocommit=False)
            yield connection
        except Exception as e:
            if connection:
                connection.rollback()
            logger.error(f"Ошибка при работе с базой данных: {e}")
            raise
        finally:
            if connection:
                if self.use_pool:
                    # Перед возвратом в пул убеждаемся, что транзакция завершена
                    if connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                        connection.rollback()
                    self.connection_pool.putconn(connection)
                else:
                    connection.close()

    def execute_query(self, query, params=None, fetch=False):
        """
        Выполнение SQL запроса
        
        Args:
            query (str): SQL запрос
            params (tuple): Параметры для запроса
            fetch (bool): Нужно ли возвращать результат
            
        Returns:
            list: Результат запроса, если fetch=True
        """
        with self.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(query, params)
                if fetch:
                    result = cur.fetchall()
                    conn.commit()  # Коммитим даже для SELECT (для завершения транзакции)
                    return result
                else:
                    conn.commit()  # ВАЖНО! Коммитим для INSERT/UPDATE/DELETE
                    return None

    def initialize_schema(self):
        """Инициализация схемы базы данных (только таблица users)"""
        try:
            with open('schema.sql', 'r', encoding='utf-8') as f:
                schema_sql = f.read()

            self.execute_query(schema_sql)
            logger.info("Схема базы данных для пользователей инициализирована")
        except Exception as e:
            logger.error(f"Ошибка при инициализации схемы: {e}")
            raise


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
    Полная инициализация системы с расширенной статистикой
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
                pr.created_at as signal_timestamp
            FROM smart_ml.predictions pr
            JOIN fas.scoring_history sh ON sh.id = pr.signal_id
            JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id 
            WHERE pr.created_at >= NOW() - (INTERVAL '1 hour' * %s)
                AND pr.prediction = true
                AND tp.contract_type_id = 1
                AND tp.exchange_id = 1
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
            'missed_profit': 0  # Сколько профита упустили
        }

        for idx, signal in enumerate(signals):
            try:
                result = process_signal_complete(
                    db,
                    signal,
                    tp_percent=tp_percent,
                    sl_percent=sl_percent
                )

                if result['success']:
                    stats['initialized'] += 1

                    if result['is_closed']:
                        if result['close_reason'] == 'take_profit':
                            stats['closed_tp'] += 1
                            stats['total_realized_profit'] += result.get('realized_pnl', 0)
                            # Считаем упущенный профит
                            if result.get('max_profit', 0) > result.get('realized_pnl', 0):
                                stats['missed_profit'] += (result['max_profit'] - result['realized_pnl'])
                        elif result['close_reason'] == 'stop_loss':
                            stats['closed_sl'] += 1
                    else:
                        stats['open'] += 1

                    stats['total_max_profit'] += result.get('max_profit', 0)
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


def process_signal_complete(db, signal, tp_percent=4.0, sl_percent=3.0,
                            position_size=100.0, leverage=5):
    """
    ПРАВИЛЬНАЯ обработка сигнала с корректным расчетом максимального профита
    """
    try:
        signal_id = signal['signal_id']
        trading_pair_id = signal['trading_pair_id']
        pair_symbol = signal['pair_symbol']
        signal_action = signal['signal_action']
        signal_timestamp = signal['signal_timestamp']

        # Получаем цену входа
        entry_price_query = """
            SELECT mark_price
            FROM public.market_data 
            WHERE trading_pair_id = %s 
                AND capture_time >= %s - INTERVAL '1 minute'
                AND capture_time <= %s + INTERVAL '1 minute'
            ORDER BY ABS(EXTRACT(EPOCH FROM (capture_time - %s))) ASC
            LIMIT 1
        """

        price_result = db.execute_query(
            entry_price_query,
            (trading_pair_id, signal_timestamp, signal_timestamp, signal_timestamp),
            fetch=True
        )

        if not price_result:
            # Ищем ближайшую цену в пределах часа
            fallback_query = """
                SELECT mark_price
                FROM public.market_data 
                WHERE trading_pair_id = %s
                    AND capture_time >= %s - INTERVAL '1 hour'
                    AND capture_time <= %s + INTERVAL '1 hour'
                ORDER BY ABS(EXTRACT(EPOCH FROM (capture_time - %s))) ASC
                LIMIT 1
            """
            price_result = db.execute_query(
                fallback_query,
                (trading_pair_id, signal_timestamp, signal_timestamp, signal_timestamp),
                fetch=True
            )

        if not price_result:
            print(f"[PROCESS] Нет цены для {pair_symbol}")
            return {'success': False}

        entry_price = float(price_result[0]['mark_price'])

        # ВАЖНО: Получаем ВСЮ историю цен, не ограничиваясь моментом закрытия
        # Берем историю до текущего момента для расчета максимального профита
        history_query = """
            SELECT capture_time, mark_price
            FROM public.market_data
            WHERE trading_pair_id = %s
                AND capture_time >= %s
                AND capture_time <= NOW()  -- До текущего момента!
            ORDER BY capture_time ASC
        """

        history = db.execute_query(history_query, (trading_pair_id, signal_timestamp), fetch=True)

        if not history:
            # Если нет истории, сохраняем с начальными данными
            insert_query = """
                INSERT INTO web.web_signals (
                    signal_id, pair_symbol, signal_action, signal_timestamp,
                    entry_price, position_size_usd, leverage,
                    trailing_stop_percent, take_profit_percent,
                    is_closed, last_known_price
                ) VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s
                )
            """
            db.execute_query(insert_query, (
                signal_id, pair_symbol, signal_action, signal_timestamp,
                entry_price, position_size, leverage, sl_percent, tp_percent, entry_price
            ))
            return {'success': True, 'is_closed': False, 'close_reason': None, 'max_profit': 0}

        # Переменные для отслеживания
        max_profit = 0
        max_profit_price = entry_price
        max_profit_time = signal_timestamp

        is_closed = False
        close_price = None
        close_time = None
        close_reason = None
        realized_pnl = 0

        # Лучшая цена для расчета максимального профита
        best_price_ever = entry_price

        # Проходим по всей истории
        for idx, price_point in enumerate(history):
            current_price = float(price_point['mark_price'])
            current_time = price_point['capture_time']

            # СНАЧАЛА обновляем лучшую цену независимо от закрытия
            if signal_action in ['SELL', 'SHORT']:
                # Для SHORT лучшая цена = минимальная
                if current_price < best_price_ever:
                    best_price_ever = current_price
                    # Рассчитываем максимальный профит
                    max_profit_percent = ((entry_price - best_price_ever) / entry_price) * 100
                    potential_max_profit = position_size * (max_profit_percent / 100) * leverage
                    if potential_max_profit > max_profit:
                        max_profit = potential_max_profit
                        max_profit_price = best_price_ever
                        max_profit_time = current_time
            else:  # BUY, LONG
                # Для LONG лучшая цена = максимальная
                if current_price > best_price_ever:
                    best_price_ever = current_price
                    # Рассчитываем максимальный профит
                    max_profit_percent = ((best_price_ever - entry_price) / entry_price) * 100
                    potential_max_profit = position_size * (max_profit_percent / 100) * leverage
                    if potential_max_profit > max_profit:
                        max_profit = potential_max_profit
                        max_profit_price = best_price_ever
                        max_profit_time = current_time

            # ПОТОМ проверяем условия закрытия (только если еще не закрыта)
            if not is_closed:
                # Рассчитываем текущий P&L
                if signal_action in ['SELL', 'SHORT']:
                    current_profit_percent = ((entry_price - current_price) / entry_price) * 100
                else:  # BUY, LONG
                    current_profit_percent = ((current_price - entry_price) / entry_price) * 100

                current_pnl = position_size * (current_profit_percent / 100) * leverage

                # Проверяем TP
                if current_profit_percent >= tp_percent:
                    is_closed = True
                    close_price = current_price
                    close_time = current_time
                    close_reason = 'take_profit'
                    realized_pnl = current_pnl
                    # НЕ прерываем цикл! Продолжаем искать максимальный профит

                # Проверяем SL
                elif current_profit_percent <= -sl_percent:
                    is_closed = True
                    close_price = current_price
                    close_time = current_time
                    close_reason = 'stop_loss'
                    realized_pnl = current_pnl
                    # НЕ прерываем цикл! Продолжаем искать максимальный профит

        # Последняя известная цена
        last_price = float(history[-1]['mark_price'])

        # Если позиция не закрылась, рассчитываем unrealized P&L
        if not is_closed:
            if signal_action in ['SELL', 'SHORT']:
                unrealized_percent = ((entry_price - last_price) / entry_price) * 100
            else:
                unrealized_percent = ((last_price - entry_price) / entry_price) * 100
            unrealized_pnl = position_size * (unrealized_percent / 100) * leverage
        else:
            unrealized_pnl = 0

        # Отладочный вывод для закрытых по TP позиций
        if close_reason == 'take_profit' and max_profit > realized_pnl * 1.1:  # Если макс профит > 110% от realized
            print(f"[PROCESS] {pair_symbol}: TP at {close_price:.8f} (profit: ${realized_pnl:.2f}), "
                  f"but max was {max_profit_price:.8f} (potential: ${max_profit:.2f})")

        # Сохраняем в БД
        insert_query = """
            INSERT INTO web.web_signals (
                signal_id,
                pair_symbol,
                signal_action,
                signal_timestamp,
                entry_price,
                position_size_usd,
                leverage,
                trailing_stop_percent,
                take_profit_percent,
                is_closed,
                closing_price,
                closed_at,
                close_reason,
                realized_pnl_usd,
                unrealized_pnl_usd,
                max_potential_profit_usd,
                last_known_price
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """

        db.execute_query(insert_query, (
            signal_id,
            pair_symbol,
            signal_action,
            signal_timestamp,
            entry_price,
            position_size,
            leverage,
            sl_percent,
            tp_percent,
            is_closed,
            close_price,
            close_time,
            close_reason,
            realized_pnl if is_closed else 0,
            unrealized_pnl if not is_closed else 0,
            max_profit,  # Это РЕАЛЬНЫЙ максимум за всю историю
            last_price
        ))

        # Для статистики
        if is_closed and close_reason == 'take_profit':
            efficiency = (realized_pnl / max_profit * 100) if max_profit > 0 else 100
            if efficiency < 80:  # Если поймали меньше 80% от максимума
                print(f"[PROCESS] {pair_symbol}: Эффективность TP = {efficiency:.1f}% "
                      f"(взяли ${realized_pnl:.2f} из возможных ${max_profit:.2f})")

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


def build_scoring_filter_conditions(filters):
    """
    Построение SQL условий из фильтров скоринга
    """
    conditions = []
    params = []

    # Отладочный вывод
    print(f"\n[DEBUG] Обработка фильтра:")
    print(f"  Order Type: {filters.get('order_type')}")
    print(f"  Market: {filters.get('market', 'Не указан')}")
    print(f"  Recommended Action: {filters.get('recommended_action', 'Не указано')}")

    # Базовое условие - начинаем с пустой скобки
    # Теперь market НЕ обязательный
    if filters.get('order_type') not in ['BUY', 'SELL']:
        print("[DEBUG] ОШИБКА: Не указан или неверный order_type!")
        return "", []

    # Собираем все условия
    all_conditions = []

    # Market - теперь опциональный
    if filters.get('market'):
        all_conditions.append("mr.regime = %s")
        params.append(str(filters['market']))
        print(f"[DEBUG] Добавлено условие market: {filters['market']}")

    # Recommended Action - опциональный
    if filters.get('recommended_action'):
        all_conditions.append("sh.recommended_action = %s")
        params.append(filters['recommended_action'])
        print(f"[DEBUG] Добавлено условие recommended_action: {filters['recommended_action']}")

    # Total score
    if filters.get('total_score_min') is not None:
        all_conditions.append("sh.total_score >= %s")
        params.append(float(filters['total_score_min']))
    if filters.get('total_score_max') is not None:
        all_conditions.append("sh.total_score <= %s")
        params.append(float(filters['total_score_max']))

    # Indicator score
    if filters.get('indicator_score_min') is not None:
        all_conditions.append("sh.indicator_score >= %s")
        params.append(float(filters['indicator_score_min']))
    if filters.get('indicator_score_max') is not None:
        all_conditions.append("sh.indicator_score <= %s")
        params.append(float(filters['indicator_score_max']))

    # Pattern score
    if filters.get('pattern_score_min') is not None:
        all_conditions.append("sh.pattern_score >= %s")
        params.append(float(filters['pattern_score_min']))
    if filters.get('pattern_score_max') is not None:
        all_conditions.append("sh.pattern_score <= %s")
        params.append(float(filters['pattern_score_max']))

    # Combination score
    if filters.get('combination_score_min') is not None:
        all_conditions.append("sh.combination_score >= %s")
        params.append(float(filters['combination_score_min']))
    if filters.get('combination_score_max') is not None:
        all_conditions.append("sh.combination_score <= %s")
        params.append(float(filters['combination_score_max']))

    # Если нет условий вообще - возвращаем пустое условие
    if not all_conditions:
        print("[DEBUG] Нет условий для фильтрации!")
        return "", []

    # Собираем полное условие
    full_condition = "(" + " AND ".join(all_conditions) + ")"

    print(f"[DEBUG] Сформированное условие: {full_condition}")
    print(f"[DEBUG] Количество параметров: {len(params)}")

    return full_condition, params


def get_scoring_signals(db, date_filter, buy_filters=None, sell_filters=None):
    """
    Получение сигналов на основе фильтров скоринга
    """

    # Сначала собираем все условия и параметры для CASE
    case_conditions = []
    case_params = []
    all_conditions = []

    # Обрабатываем BUY фильтры
    if buy_filters:
        for idx, filter_set in enumerate(buy_filters):
            condition, filter_params = build_scoring_filter_conditions(filter_set)
            if condition:
                case_conditions.append(f"WHEN {condition} THEN 'BUY'")
                all_conditions.append(condition)
                case_params.extend(filter_params)

    # Обрабатываем SELL фильтры
    if sell_filters:
        for idx, filter_set in enumerate(sell_filters):
            condition, filter_params = build_scoring_filter_conditions(filter_set)
            if condition:
                case_conditions.append(f"WHEN {condition} THEN 'SELL'")
                all_conditions.append(condition)
                case_params.extend(filter_params)

    # Если нет фильтров, возвращаем пустой список
    if not case_conditions:
        print("[DEBUG] Нет активных фильтров")
        return []

    # Строим полный запрос
    query = """
        SELECT
            sh.*,
            tp.pair_symbol as symbol,
            mr.regime AS market_regime,
            sh.recommended_action,
            CASE 
    """

    query += "\n".join(case_conditions) + """
            END as signal_action
        FROM fas.scoring_history AS sh
        JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
        LEFT JOIN LATERAL (
            SELECT regime
            FROM fas.market_regime mr
            WHERE mr.timestamp <= sh.timestamp
                AND mr.timeframe = '4h'
            ORDER BY mr.timestamp DESC
            LIMIT 1
        ) AS mr ON true
        WHERE sh.timestamp::date = %s
            AND tp.contract_type_id = 1
            AND tp.exchange_id = 1
            AND (
    """

    query += " OR ".join(all_conditions) + ")"
    query += " ORDER BY sh.timestamp DESC"

    # Собираем все параметры в правильном порядке
    all_params = []
    all_params.extend(case_params)  # Параметры для CASE
    all_params.append(date_filter)  # Дата для WHERE
    all_params.extend(case_params)  # Те же параметры для WHERE условий

    # ОТЛАДОЧНЫЙ ВЫВОД
    print("\n" + "=" * 80)
    print("[DEBUG] ИТОГОВЫЙ SQL ЗАПРОС (с плейсхолдерами %s):")
    print("=" * 80)
    print(query)
    print("\n[DEBUG] ПАРАМЕТРЫ:")
    for i, param in enumerate(all_params):
        param_type = type(param).__name__
        print(f"  Параметр {i + 1}: {param} (тип: {param_type})")

    # СОЗДАЕМ SQL С ПОДСТАВЛЕННЫМИ ЗНАЧЕНИЯМИ ДЛЯ ОТЛАДКИ
    debug_query = query
    # Заменяем все %s на реальные значения по порядку
    for param in all_params:
        if isinstance(param, str):
            value = f"'{param}'"
        elif param is None:
            value = "NULL"
        elif isinstance(param, (int, float)):
            value = str(param)
        else:
            value = f"'{param}'"

        # Заменяем первое вхождение %s
        debug_query = debug_query.replace('%s', value, 1)

    print("\n" + "=" * 80)
    print("[DEBUG] SQL С ПОДСТАВЛЕННЫМИ ЗНАЧЕНИЯМИ (для проверки в БД):")
    print("=" * 80)
    print(debug_query)
    print("=" * 80)

    # Дополнительная проверка - убедимся что все %s заменены
    remaining_placeholders = debug_query.count('%s')
    if remaining_placeholders > 0:
        print(f"[WARNING] Остались незамененные плейсхолдеры: {remaining_placeholders}")
        print(f"[WARNING] Всего параметров: {len(all_params)}")
        print(f"[WARNING] Плейсхолдеров в исходном запросе: {query.count('%s')}")

    print("\n[DEBUG] Выполняем запрос...")

    # Выполняем запрос
    try:
        results = db.execute_query(query, tuple(all_params), fetch=True)
        print(f"[DEBUG] Найдено сигналов: {len(results) if results else 0}")

        if results and len(results) > 0:
            print("\n[DEBUG] Примеры найденных сигналов (первые 5):")
            for i, signal in enumerate(results[:5]):
                print(f"  {i + 1}. {signal.get('symbol', 'N/A'):10s} | "
                      f"Time: {signal.get('timestamp').strftime('%H:%M') if signal.get('timestamp') else 'N/A'} | "
                      f"Total: {float(signal.get('total_score', 0)):6.1f} | "
                      f"Ind: {float(signal.get('indicator_score', 0)):6.1f} | "
                      f"Pat: {float(signal.get('pattern_score', 0)):6.1f} | "
                      f"Market: {signal.get('market_regime', 'N/A'):7s}")

        return results
    except Exception as e:
        print(f"[DEBUG] ОШИБКА выполнения запроса: {e}")
        import traceback
        print(traceback.format_exc())
        raise


def process_scoring_signals_batch(db, signals, session_id, user_id,
                                  tp_percent=4.0, sl_percent=3.0,
                                  position_size=100.0, leverage=5):
    """
    Пакетная обработка сигналов скоринга с сохранением в БД
    Анализирует только 48 часов после каждого сигнала
    """

    # Очищаем предыдущие результаты для этой сессии
    clear_query = """
        DELETE FROM web.scoring_analysis_results 
        WHERE session_id = %s AND user_id = %s
    """
    db.execute_query(clear_query, (session_id, user_id))

    print(f"[SCORING] Начинаем обработку {len(signals)} сигналов...")
    print(f"[SCORING] Параметры: TP={tp_percent}%, SL={sl_percent}%, Size=${position_size}, Lev={leverage}x")

    processed_count = 0
    error_count = 0

    # Подготавливаем данные для batch insert
    batch_data = []

    for idx, signal in enumerate(signals):
        try:
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

            price_result = db.execute_query(
                entry_price_query,
                (signal['trading_pair_id'], signal['timestamp'],
                 signal['timestamp'], signal['timestamp']),
                fetch=True
            )

            if not price_result:
                continue

            entry_price = float(price_result[0]['mark_price'])

            # ВАЖНО: Получаем историю ТОЛЬКО за 48 часов после сигнала
            history_query = """
                SELECT capture_time, mark_price
                FROM public.market_data
                WHERE trading_pair_id = %s
                    AND capture_time >= %s
                    AND capture_time <= %s + INTERVAL '48 hours'
                ORDER BY capture_time ASC
            """

            history = db.execute_query(
                history_query,
                (signal['trading_pair_id'], signal['timestamp'], signal['timestamp']),
                fetch=True
            )

            if not history:
                continue

            # Переменные для отслеживания ЗАКРЫТИЯ позиции
            is_closed = False
            close_reason = None
            close_price = None
            close_time = None
            hours_to_close = None

            # Переменные для отслеживания МАКСИМАЛЬНОГО профита (независимо от закрытия)
            best_price = entry_price
            max_profit_percent = 0
            max_profit_usd = 0

            # Проходим по всей истории
            for price_point in history:
                current_price = float(price_point['mark_price'])
                current_time = price_point['capture_time']
                hours_passed = (current_time - signal['timestamp']).total_seconds() / 3600

                # ВСЕГДА обновляем лучшую цену для расчета максимального профита
                if signal['signal_action'] == 'SELL':
                    if current_price < best_price:
                        best_price = current_price
                        temp_profit_percent = ((entry_price - best_price) / entry_price) * 100
                        temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                        if temp_profit_usd > max_profit_usd:
                            max_profit_percent = temp_profit_percent
                            max_profit_usd = temp_profit_usd
                else:  # BUY
                    if current_price > best_price:
                        best_price = current_price
                        temp_profit_percent = ((best_price - entry_price) / entry_price) * 100
                        temp_profit_usd = position_size * (temp_profit_percent / 100) * leverage
                        if temp_profit_usd > max_profit_usd:
                            max_profit_percent = temp_profit_percent
                            max_profit_usd = temp_profit_usd

                # Проверяем условия закрытия (только если еще не закрыта)
                # ВАЖНО: Фиксируем параметры закрытия только ОДИН РАЗ!
                if not is_closed:
                    if signal['signal_action'] == 'SELL':
                        price_change_percent = ((entry_price - current_price) / entry_price) * 100
                    else:  # BUY
                        price_change_percent = ((current_price - entry_price) / entry_price) * 100

                    # Проверяем TP
                    if price_change_percent >= tp_percent:
                        is_closed = True
                        close_reason = 'take_profit'
                        close_price = current_price  # Фиксируем цену закрытия
                        close_time = current_time  # Фиксируем время закрытия
                        hours_to_close = hours_passed  # Фиксируем время до закрытия
                        # Продолжаем цикл для поиска максимального профита, но НЕ меняем параметры закрытия

                    # Проверяем SL
                    elif price_change_percent <= -sl_percent:
                        is_closed = True
                        close_reason = 'stop_loss'
                        close_price = current_price  # Фиксируем цену закрытия
                        close_time = current_time  # Фиксируем время закрытия
                        hours_to_close = hours_passed  # Фиксируем время до закрытия
                        # Продолжаем цикл для поиска максимального профита, но НЕ меняем параметры закрытия

            # Если не закрылась за 48 часов - закрываем по таймауту
            if not is_closed:
                is_closed = True
                close_reason = 'timeout'
                close_price = float(history[-1]['mark_price'])
                close_time = history[-1]['capture_time']
                hours_to_close = 48.0

            # Рассчитываем финальный P&L на момент закрытия (используем зафиксированную close_price)
            if signal['signal_action'] == 'SELL':
                final_pnl_percent = ((entry_price - close_price) / entry_price) * 100
            else:
                final_pnl_percent = ((close_price - entry_price) / entry_price) * 100

            final_pnl_usd = position_size * (final_pnl_percent / 100) * leverage

            # Отладочная информация
            if close_reason == 'take_profit':
                if abs(final_pnl_percent - tp_percent) > 0.5:  # Если отклонение больше 0.5%
                    print(f"[WARNING] {signal['symbol']}: TP отклонение! "
                          f"Ожидалось {tp_percent}%, получилось {final_pnl_percent:.2f}%")

                if max_profit_usd > final_pnl_usd * 1.2:  # Если макс профит больше на 20%
                    print(f"[INFO] {signal['symbol']}: Упущенный профит! "
                          f"TP=${final_pnl_usd:.2f} ({final_pnl_percent:.2f}%), "
                          f"Макс=${max_profit_usd:.2f} ({max_profit_percent:.2f}%)")

            # Добавляем в batch
            batch_data.append((
                session_id,
                user_id,
                signal['timestamp'],
                signal['symbol'],
                signal['trading_pair_id'],
                signal['signal_action'],
                signal['market_regime'],
                float(signal.get('total_score', 0)),
                float(signal.get('indicator_score', 0)),
                float(signal.get('pattern_score', 0)),
                float(signal.get('combination_score', 0)),
                entry_price,
                best_price,
                close_price,  # Используем зафиксированную цену закрытия
                close_time,  # Используем зафиксированное время закрытия
                is_closed,
                close_reason,
                hours_to_close,  # Используем зафиксированное время до закрытия
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

            # Вставляем пачками по 100
            if len(batch_data) >= 100:
                _insert_batch_results(db, batch_data)
                batch_data = []
                print(f"[SCORING] Обработано {processed_count}/{len(signals)} сигналов...")

        except Exception as e:
            error_count += 1
            print(f"[SCORING] Ошибка обработки сигнала {signal.get('symbol', 'UNKNOWN')}: {e}")
            continue

    # Вставляем оставшиеся данные
    if batch_data:
        _insert_batch_results(db, batch_data)

    print(f"[SCORING] Обработка завершена: {processed_count} успешно, {error_count} ошибок")

    # Возвращаем статистику
    stats_query = """
        SELECT 
            COUNT(*) as total,
            COUNT(CASE WHEN signal_action = 'BUY' THEN 1 END) as buy_signals,
            COUNT(CASE WHEN signal_action = 'SELL' THEN 1 END) as sell_signals,
            COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as tp_count,
            COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
            COUNT(CASE WHEN close_reason = 'timeout' THEN 1 END) as timeout_count,
            SUM(pnl_usd) as total_pnl,
            SUM(CASE WHEN close_reason = 'take_profit' THEN pnl_usd ELSE 0 END) as tp_profit,
            SUM(CASE WHEN close_reason = 'stop_loss' THEN ABS(pnl_usd) ELSE 0 END) as sl_loss,
            SUM(max_potential_profit_usd) as total_max_potential,
            AVG(hours_to_close) FILTER (WHERE close_reason != 'timeout') as avg_hours_to_close,

            -- Метрики упущенного профита
            SUM(max_potential_profit_usd - pnl_usd) FILTER (WHERE close_reason = 'take_profit') as missed_profit_tp,
            AVG((max_potential_profit_usd - pnl_usd) / NULLIF(pnl_usd, 0) * 100) 
                FILTER (WHERE close_reason = 'take_profit' AND pnl_usd > 0) as avg_missed_percent
        FROM web.scoring_analysis_results
        WHERE session_id = %s AND user_id = %s
    """

    stats = db.execute_query(stats_query, (session_id, user_id), fetch=True)[0]

    return {
        'processed': processed_count,
        'errors': error_count,
        'stats': stats
    }


def _insert_batch_results(db, batch_data):
    """Вспомогательная функция для batch insert"""
    insert_query = """
        INSERT INTO web.scoring_analysis_results (
            session_id, user_id, signal_timestamp, pair_symbol, trading_pair_id,
            signal_action, market_regime,
            total_score, indicator_score, pattern_score, combination_score,
            entry_price, best_price, close_price, close_time,
            is_closed, close_reason, hours_to_close,
            pnl_percent, pnl_usd,
            max_potential_profit_percent, max_potential_profit_usd,
            tp_percent, sl_percent, position_size, leverage
        ) VALUES %s
    """

    # psycopg3 поддерживает execute_values через другой синтаксис
    # Используем execute_batch или построим запрос вручную
    for data in batch_data:
        single_insert = """
            INSERT INTO web.scoring_analysis_results (
                session_id, user_id, signal_timestamp, pair_symbol, trading_pair_id,
                signal_action, market_regime,
                total_score, indicator_score, pattern_score, combination_score,
                entry_price, best_price, close_price, close_time,
                is_closed, close_reason, hours_to_close,
                pnl_percent, pnl_usd,
                max_potential_profit_percent, max_potential_profit_usd,
                tp_percent, sl_percent, position_size, leverage
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        db.execute_query(single_insert, data)


def get_scoring_analysis_results(db, session_id, user_id):
    """Получение результатов анализа из БД"""
    query = """
        SELECT * FROM web.scoring_analysis_results
        WHERE session_id = %s AND user_id = %s
        ORDER BY signal_timestamp DESC
    """
    return db.execute_query(query, (session_id, user_id), fetch=True)


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


















