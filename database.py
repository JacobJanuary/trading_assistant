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




















