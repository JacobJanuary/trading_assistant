# ПЛАН РЕАЛИЗАЦИИ ФИЛЬТРА ПО БИРЖАМ ДЛЯ signal_performance

**Дата создания:** 2025-11-12
**Ветка:** feature/add-exchange-filter
**Цель:** Добавить возможность фильтрации сигналов по биржам (Binance, Bybit) в разделе "Сигналы" (signal_performance)

---

## ТЕКУЩЕЕ СОСТОЯНИЕ

✅ **Сохранено в git:**
- Коммит: `0a699ae` - Add helper functions for public.candles migration
- Ветка: `feature/migrate-to-public-candles` - слита
- Новая ветка: `feature/add-exchange-filter` - создана

✅ **Проведен аудит:**
- Изучена архитектура проекта
- Проанализирована текущая реализация signal_performance
- Определены необходимые изменения
- Составлен технический план

---

## ФАЗЫ РЕАЛИЗАЦИИ

### 📋 ФАЗА 0: ПОДГОТОВКА И ПЛАНИРОВАНИЕ (ЗАВЕРШЕНА)

**Задачи:**
- [x] Создать ветку feature/add-exchange-filter
- [x] Провести аудит кодовой базы
- [x] Составить детальный план реализации
- [x] Создать документацию плана

**Тестирование:** N/A

**Git checkpoint:** Начальная точка на чистой ветке

---

### 🗄️ ФАЗА 1: МИГРАЦИЯ БАЗЫ ДАННЫХ

**Цель:** Добавить поддержку фильтрации по биржам на уровне БД

#### Задачи:

**1.1. Создать SQL скрипт миграции**
- [ ] Создать файл `migrations/001_add_exchange_filter.sql`
- [ ] Добавить колонку `selected_exchanges` в `web.user_signal_filters`
- [ ] Добавить колонку `exchange_id` в `web.web_signals`
- [ ] Создать индексы для производительности
- [ ] Добавить комментарии к колонкам

**1.2. Заполнить exchange_id для существующих записей**
- [ ] Написать UPDATE запрос для заполнения `web.web_signals.exchange_id`
- [ ] Проверить что все записи получили exchange_id

**1.3. Установить значения по умолчанию**
- [ ] Обновить `selected_exchanges` для существующих пользователей

#### SQL скрипт (`migrations/001_add_exchange_filter.sql`):

```sql
-- ============================================================================
-- MIGRATION: Add Exchange Filter Support
-- Date: 2025-11-12
-- Description: Добавляет возможность фильтрации сигналов по биржам
-- ============================================================================

BEGIN;

-- 1. Добавляем колонку selected_exchanges в user_signal_filters
ALTER TABLE web.user_signal_filters
ADD COLUMN IF NOT EXISTS selected_exchanges INTEGER[] DEFAULT ARRAY[1, 2];

COMMENT ON COLUMN web.user_signal_filters.selected_exchanges IS
'Массив ID бирж для фильтрации (Binance=1, Bybit=2, Coinbase=3). По умолчанию [1,2]';

-- 2. Добавляем колонку exchange_id в web_signals
ALTER TABLE web.web_signals
ADD COLUMN IF NOT EXISTS exchange_id INTEGER;

-- Добавляем foreign key (без NOT NULL пока не заполним данные)
ALTER TABLE web.web_signals
DROP CONSTRAINT IF EXISTS fk_web_signals_exchange;

ALTER TABLE web.web_signals
ADD CONSTRAINT fk_web_signals_exchange
FOREIGN KEY (exchange_id) REFERENCES public.exchanges(id);

-- Создаем индекс для производительности
CREATE INDEX IF NOT EXISTS idx_web_signals_exchange_id
ON web.web_signals(exchange_id);

-- Создаем составной индекс для частых запросов
CREATE INDEX IF NOT EXISTS idx_web_signals_exchange_timestamp
ON web.web_signals(exchange_id, signal_timestamp DESC);

COMMENT ON COLUMN web.web_signals.exchange_id IS
'ID биржи из public.exchanges (1=Binance, 2=Bybit, 3=Coinbase)';

-- 3. Заполняем exchange_id для существующих записей
-- Используем DISTINCT ON для выбора одной биржи на pair_symbol
UPDATE web.web_signals ws
SET exchange_id = subq.exchange_id
FROM (
    SELECT DISTINCT ON (tp.pair_symbol)
        tp.pair_symbol,
        tp.exchange_id
    FROM public.trading_pairs tp
    WHERE tp.contract_type_id = 1  -- FUTURES
      AND tp.is_active = true
    ORDER BY tp.pair_symbol, tp.exchange_id  -- Приоритет Binance (id=1)
) subq
WHERE ws.pair_symbol = subq.pair_symbol
  AND ws.exchange_id IS NULL;

-- 4. Проверяем результаты миграции
DO $$
DECLARE
    total_signals INT;
    signals_with_exchange INT;
    signals_without_exchange INT;
BEGIN
    SELECT
        COUNT(*),
        COUNT(exchange_id),
        COUNT(*) - COUNT(exchange_id)
    INTO total_signals, signals_with_exchange, signals_without_exchange
    FROM web.web_signals;

    RAISE NOTICE 'Migration results:';
    RAISE NOTICE '  Total signals: %', total_signals;
    RAISE NOTICE '  With exchange_id: %', signals_with_exchange;
    RAISE NOTICE '  Without exchange_id: %', signals_without_exchange;

    IF signals_without_exchange > 0 THEN
        RAISE WARNING 'Found % signals without exchange_id! Check pair_symbol mappings.', signals_without_exchange;
    END IF;
END $$;

-- 5. Обновляем selected_exchanges для всех существующих пользователей
UPDATE web.user_signal_filters
SET selected_exchanges = ARRAY[1, 2]
WHERE selected_exchanges IS NULL;

-- 6. Проверяем структуру таблиц
SELECT
    'user_signal_filters' as table_name,
    column_name,
    data_type,
    column_default
FROM information_schema.columns
WHERE table_schema = 'web'
  AND table_name = 'user_signal_filters'
  AND column_name = 'selected_exchanges'
UNION ALL
SELECT
    'web_signals' as table_name,
    column_name,
    data_type,
    column_default
FROM information_schema.columns
WHERE table_schema = 'web'
  AND table_name = 'web_signals'
  AND column_name = 'exchange_id';

COMMIT;

-- Rollback script (если нужно откатить миграцию):
-- BEGIN;
-- ALTER TABLE web.web_signals DROP COLUMN IF EXISTS exchange_id CASCADE;
-- ALTER TABLE web.user_signal_filters DROP COLUMN IF EXISTS selected_exchanges;
-- COMMIT;
```

#### Тестирование ФАЗА 1:

**Тест 1.1: Проверка добавления колонок**
```sql
-- Проверяем что колонки созданы
\d web.user_signal_filters
\d web.web_signals

-- Ожидаемый результат:
-- user_signal_filters.selected_exchanges: integer[] | default: ARRAY[1,2]
-- web_signals.exchange_id: integer | nullable
```

**Тест 1.2: Проверка заполнения exchange_id**
```sql
-- Проверяем распределение по биржам
SELECT
    e.exchange_name,
    COUNT(ws.*) as signal_count,
    COUNT(ws.exchange_id) as filled_count
FROM web.web_signals ws
LEFT JOIN public.exchanges e ON e.id = ws.exchange_id
GROUP BY e.exchange_name
ORDER BY e.id;

-- Ожидаемый результат:
-- Binance: N сигналов, N заполнено
-- Bybit: M сигналов, M заполнено
-- NULL: 0 сигналов (все должны быть заполнены)
```

**Тест 1.3: Проверка индексов**
```sql
-- Проверяем что индексы созданы
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'web_signals'
  AND indexname LIKE '%exchange%';

-- Ожидаемый результат:
-- idx_web_signals_exchange_id
-- idx_web_signals_exchange_timestamp
```

**Тест 1.4: Проверка производительности**
```sql
-- Проверяем что запрос использует индекс
EXPLAIN ANALYZE
SELECT *
FROM web.web_signals
WHERE exchange_id = 1
  AND signal_timestamp >= NOW() - INTERVAL '48 hours'
ORDER BY signal_timestamp DESC
LIMIT 100;

-- Ожидаемый результат:
-- Index Scan using idx_web_signals_exchange_timestamp
-- Execution time < 50ms
```

**Критерии успеха ФАЗА 1:**
- ✅ Все колонки созданы без ошибок
- ✅ Все существующие записи в web_signals имеют exchange_id
- ✅ Индексы созданы и используются
- ✅ Дефолтные значения установлены для пользователей
- ✅ Нет сигналов с exchange_id = NULL

**Git checkpoint 1:**
```bash
git add migrations/001_add_exchange_filter.sql
git commit -m "Phase 1: Add exchange filter database schema

- Add selected_exchanges column to user_signal_filters
- Add exchange_id column to web_signals
- Create indexes for performance
- Populate exchange_id for existing records
- Add rollback script"
```

---

### 🔧 ФАЗА 2: ОБНОВЛЕНИЕ BACKEND (database.py)

**Цель:** Обновить функции обработки сигналов для поддержки фильтрации по биржам

#### Задачи:

**2.1. Обновить `get_best_scoring_signals_with_backtest_params()`**
- [ ] Добавить параметр `selected_exchanges=None`
- [ ] Заменить жесткую фильтрацию на динамическую
- [ ] Обновить SQL запрос для работы с массивом exchange_id
- [ ] Добавить логирование выбранных бирж

**2.2. Обновить `process_signal_complete()`**
- [ ] Добавить параметр `exchange_id=None`
- [ ] Сохранять exchange_id в INSERT запросах
- [ ] Обновить все вызовы функции

**2.3. Добавить функцию валидации**
- [ ] Создать `validate_exchange_ids()` для проверки ID бирж

#### Код изменений:

**Файл: `database.py`**

**Изменение 1: database.py:3738** - Обновление сигнатуры функции
```python
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
```

**Изменение 2: database.py:3900** - Динамическая фильтрация по биржам
```python
    WHERE
        sc.timestamp >= NOW() - INTERVAL '24 hours'
        AND sc.is_active = true
        AND tp.is_active = true
        AND tp.contract_type_id = 1
        AND tp.exchange_id = ANY(%s)  -- Динамическая фильтрация
        AND sc.score_week > bp.score_week_filter
        AND sc.score_month > bp.score_month_filter
        AND EXTRACT(HOUR FROM sc.timestamp) NOT BETWEEN 0 AND 1
    """

    query += " ORDER BY sc.timestamp DESC"

    try:
        # Передаем selected_exchanges как параметр
        results = db.execute_query(query, (selected_exchanges,), fetch=True)
```

**Изменение 3: database.py:1281** - Обновление process_signal_complete()
```python
def process_signal_complete(db, signal,
                            tp_percent=None, sl_percent=None,
                            position_size=None, leverage=None,
                            use_trailing_stop=None,
                            trailing_distance_pct=None,
                            trailing_activation_pct=None,
                            exchange_id=None):  # НОВЫЙ ПАРАМЕТР
    """
    Обработка сигнала с поддержкой Trailing Stop
    Использует дефолтные значения из Config если параметры не переданы

    Args:
        exchange_id: ID биржи из public.exchanges (опционально, берется из signal)
    """
    # ... существующий код ...

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
```

**Изменение 4: database.py:1362-1381** - Добавление exchange_id в INSERT
```python
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
            score_week, score_month, exchange_id  # ДОБАВЛЕНО
        ))
```

**Изменение 5: database.py:1560+** - Обновление других INSERT запросов
```python
# Найти все INSERT INTO web.web_signals и добавить exchange_id
# Всего ~3-4 места в функции process_signal_complete
```

**Новая функция: database.py:конец файла** - Валидация exchange_ids
```python
def validate_exchange_ids(db, exchange_ids):
    """
    Проверяет что все переданные ID бирж существуют в public.exchanges

    Args:
        db: Database instance
        exchange_ids: list[int] - список ID бирж для проверки

    Returns:
        tuple: (is_valid: bool, valid_ids: list, invalid_ids: list)
    """
    if not exchange_ids:
        return False, [], []

    try:
        query = "SELECT id FROM public.exchanges WHERE id = ANY(%s)"
        results = db.execute_query(query, (exchange_ids,), fetch=True)

        valid_ids = [r['id'] for r in results] if results else []
        invalid_ids = [eid for eid in exchange_ids if eid not in valid_ids]

        is_valid = len(invalid_ids) == 0

        return is_valid, valid_ids, invalid_ids
    except Exception as e:
        print(f"[VALIDATE] Ошибка валидации exchange_ids: {e}")
        return False, [], exchange_ids
```

#### Тестирование ФАЗА 2:

**Тест 2.1: Импорт модулей**
```bash
python3 -c "from database import get_best_scoring_signals_with_backtest_params, process_signal_complete, validate_exchange_ids; print('Import OK')"

# Ожидаемый результат: Import OK
```

**Тест 2.2: Валидация exchange_ids**
```python
# Создать test_exchange_filter.py
from database import Database, validate_exchange_ids

db = Database()

# Тест валидации
is_valid, valid, invalid = validate_exchange_ids(db, [1, 2])
assert is_valid == True
assert valid == [1, 2]
assert invalid == []

is_valid, valid, invalid = validate_exchange_ids(db, [1, 999])
assert is_valid == False
assert 999 in invalid

print("✅ Validation tests passed")
```

**Тест 2.3: Получение сигналов с фильтрацией**
```python
# test_exchange_filter.py
from database import Database, get_best_scoring_signals_with_backtest_params

db = Database()

# Тест 1: Только Binance
signals_binance, params_binance = get_best_scoring_signals_with_backtest_params(db, [1])
assert all(s.get('exchange_id') == 1 for s in signals_binance), "All signals should be from Binance"
assert 1 in params_binance, "Should have params for Binance"
assert 2 not in params_binance, "Should not have params for Bybit"
print(f"✅ Binance only: {len(signals_binance)} signals")

# Тест 2: Только Bybit
signals_bybit, params_bybit = get_best_scoring_signals_with_backtest_params(db, [2])
assert all(s.get('exchange_id') == 2 for s in signals_bybit), "All signals should be from Bybit"
assert 2 in params_bybit, "Should have params for Bybit"
assert 1 not in params_bybit, "Should not have params for Binance"
print(f"✅ Bybit only: {len(signals_bybit)} signals")

# Тест 3: Обе биржи
signals_both, params_both = get_best_scoring_signals_with_backtest_params(db, [1, 2])
assert len(signals_both) >= len(signals_binance), "Should have at least Binance signals"
assert len(signals_both) >= len(signals_bybit), "Should have at least Bybit signals"
assert 1 in params_both and 2 in params_both, "Should have params for both"
print(f"✅ Both exchanges: {len(signals_both)} signals")

print("✅ All backend tests passed")
```

**Критерии успеха ФАЗА 2:**
- ✅ Код компилируется без ошибок
- ✅ Валидация exchange_ids работает корректно
- ✅ Фильтрация по одной бирже возвращает только её сигналы
- ✅ Фильтрация по нескольким биржам работает
- ✅ exchange_id сохраняется в web_signals

**Git checkpoint 2:**
```bash
git add database.py
git commit -m "Phase 2: Update backend for exchange filtering

- Add selected_exchanges parameter to get_best_scoring_signals_with_backtest_params()
- Update SQL query for dynamic exchange filtering
- Add exchange_id parameter to process_signal_complete()
- Add exchange_id to all INSERT queries
- Add validate_exchange_ids() helper function
- Add unit tests for exchange filtering"
```

---

### 🌐 ФАЗА 3: ОБНОВЛЕНИЕ API ENDPOINTS (app.py)

**Цель:** Обновить Flask endpoints для работы с фильтром по биржам

#### Задачи:

**3.1. Обновить `/signal_performance`**
- [ ] Читать selected_exchanges из user_signal_filters
- [ ] Передавать в get_best_scoring_signals_with_backtest_params
- [ ] Добавить exchange_id в signals_data для отображения
- [ ] Обновить display_signals_query с фильтрацией

**3.2. Обновить `/api/save_filters`**
- [ ] Читать selected_exchanges из request body
- [ ] Валидировать ID бирж
- [ ] Сохранять в базу данных
- [ ] Возвращать статус

#### Код изменений:

**Файл: `app.py`**

**Изменение 1: app.py:576** - Чтение selected_exchanges и вызов функции
```python
        # ========== ИСПОЛЬЗУЕМ АВТОМАТИЧЕСКИЙ ПОДБОР ОПТИМАЛЬНЫХ ПАРАМЕТРОВ ИЗ БЭКТЕСТОВ ==========
        from database import get_best_scoring_signals_with_backtest_params
        from datetime import datetime, date, timedelta

        # Получаем selected_exchanges из фильтров пользователя
        selected_exchanges = filters.get('selected_exchanges', [1, 2])

        print(f"[SIGNAL_PERFORMANCE] Получаем сигналы с оптимальными параметрами из бэктестов")
        print(f"[SIGNAL_PERFORMANCE] Выбранные биржи: {selected_exchanges}")
        print(f"[SIGNAL_PERFORMANCE] Период: последние 48 часов")
        print(f"[SIGNAL_PERFORMANCE] Все параметры (SL, TS, max_trades) берутся из оптимальных backtest для каждой биржи")

        raw_signals, params_by_exchange = get_best_scoring_signals_with_backtest_params(
            db,
            selected_exchanges=selected_exchanges
        )
```

**Изменение 2: app.py:664-673** - Передача exchange_id в signal_data
```python
                    # Подготавливаем данные сигнала
                    signal_data = {
                        'signal_id': signal['signal_id'],
                        'pair_symbol': signal['pair_symbol'],
                        'trading_pair_id': signal['trading_pair_id'],
                        'signal_action': signal['signal_action'],
                        'signal_timestamp': make_aware(signal['timestamp']),
                        'exchange_name': signal.get('exchange_name', 'Unknown'),
                        'exchange_id': signal.get('exchange_id'),  # ДОБАВЛЕНО
                        'score_week': signal.get('score_week', 0),
                        'score_month': signal.get('score_month', 0)
                    }
```

**Изменение 3: app.py:680-690** - Передача exchange_id в process_signal_complete
```python
                    # Обрабатываем сигнал с оптимальными параметрами из бэктеста для биржи
                    result = process_signal_complete(
                        db,
                        signal_data,
                        tp_percent=float(filters.get('take_profit_percent') or 4.0),
                        sl_percent=exchange_params.get('stop_loss_filter', 3.0),
                        position_size=display_position_size,
                        leverage=display_leverage,
                        use_trailing_stop=True,  # Всегда используем TS (параметры из backtest)
                        trailing_distance_pct=exchange_params.get('trailing_distance_filter', 2.0),
                        trailing_activation_pct=exchange_params.get('trailing_activation_filter', 1.0),
                        exchange_id=signal_data.get('exchange_id')  # ДОБАВЛЕНО
                    )
```

**Изменение 4: app.py:726-733** - Фильтрация display_signals по exchange_id
```python
            # Теперь получаем обработанные сигналы из web_signals для отображения
            # с фильтрацией по возрасту и биржам
            display_signals_query = """
                SELECT *
                FROM web.web_signals
                WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                    AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                    AND exchange_id = ANY(%s)
                ORDER BY signal_timestamp DESC
            """

            display_signals = db.execute_query(
                display_signals_query,
                (hide_older, hide_younger, selected_exchanges),  # ОБНОВЛЕНО
                fetch=True
            )
```

**Изменение 5: app.py:767-787** - Добавление exchange_name в signals_data
```python
                    signal_data = {
                        'pair_symbol': signal['pair_symbol'],
                        'signal_action': signal['signal_action'],
                        'timestamp': signal['signal_timestamp'],
                        'age_hours': round(age_hours, 1),
                        'entry_price': entry_price,
                        'current_price': current_price,
                        'is_closed': signal['is_closed'],
                        'close_reason': signal['close_reason'],
                        'pnl_usd': display_pnl,
                        'pnl_percent': price_change_percent,
                        'max_potential_profit_usd': max_profit,
                        'score_week': float(signal.get('score_week', 0)),
                        'score_month': float(signal.get('score_month', 0)),
                        'exchange_id': signal.get('exchange_id'),  # ДОБАВЛЕНО
                        'exchange_name': get_exchange_name(signal.get('exchange_id')),  # ДОБАВЛЕНО
                        'status': 'open' if not signal['is_closed'] else
                                 ('tp' if signal['close_reason'] == 'take_profit' else
                                  ('sl' if signal['close_reason'] == 'stop_loss' else
                                   ('trailing' if signal['close_reason'] == 'trailing_stop' else 'closed')))
                    }
```

**Изменение 6: app.py (перед route)** - Хелпер функция get_exchange_name
```python
# Хелпер для получения имени биржи
def get_exchange_name(exchange_id):
    """Возвращает имя биржи по ID"""
    exchange_names = {
        1: 'Binance',
        2: 'Bybit',
        3: 'Coinbase'
    }
    return exchange_names.get(exchange_id, 'Unknown')
```

**Изменение 7: app.py:822-827** - Обновление efficiency_query с фильтрацией
```python
            # Рассчитываем статистику эффективности с учетом фильтра по биржам
            efficiency_query = """
                WITH signal_stats AS (
                    SELECT
                        COUNT(*) as total_signals,
                        COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_positions,
                        -- ... остальные поля ...
                    FROM web.web_signals
                    WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                        AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                        AND exchange_id = ANY(%s)  -- ДОБАВЛЕНО
                )
                SELECT * FROM signal_stats
            """

            eff_stats = db.execute_query(
                efficiency_query,
                (hide_older, hide_younger, selected_exchanges),  # ОБНОВЛЕНО
                fetch=True
            )
```

**Изменение 8: app.py:939** - Обновление trailing_query с фильтрацией
```python
                trailing_query = """
                    SELECT
                        -- ... все поля ...
                    FROM web.web_signals
                    WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                        AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                        AND exchange_id = ANY(%s)  -- ДОБАВЛЕНО
                """

                trailing_stats = db.execute_query(
                    trailing_query,
                    (hide_older, hide_younger, selected_exchanges),  # ОБНОВЛЕНО
                    fetch=True
                )
```

**Изменение 9: app.py:1013** - Передача selected_exchanges в шаблон
```python
        return render_template(
            'signal_performance.html',
            signals=signals_data,
            stats=stats,
            efficiency=efficiency_metrics,
            total_stats=total_stats,
            filters={
                'hide_younger_than_hours': hide_younger,
                'hide_older_than_hours': hide_older,
                'stop_loss_percent': float(filters.get('stop_loss_percent') or 3.0),
                'take_profit_percent': float(filters.get('take_profit_percent') or 4.0),
                'position_size_usd': display_position_size,
                'leverage': display_leverage,
                'saved_leverage': filters.get('leverage') or 5,
                'saved_position_size': float(filters.get('position_size_usd') or 100.0),
                'use_trailing_stop': True,
                'trailing_distance_pct': 0.0,
                'trailing_activation_pct': 0.0,
                'score_week_min': 0,
                'score_month_min': 0,
                'allowed_hours': list(range(24)),
                'max_trades_per_15min': 0,
                'selected_exchanges': selected_exchanges  # ДОБАВЛЕНО
            },
            last_update=datetime.now()
        )
```

**Изменение 10: app.py:1813-1853** - Обновление /api/save_filters
```python
@app.route('/api/save_filters', methods=['POST'])
@login_required
def api_save_filters():
    """Сохранение настроек фильтров пользователя"""
    try:
        data = request.get_json()

        # Валидация базовых параметров
        hide_younger = max(0, min(48, data.get('hide_younger_than_hours', 6)))
        hide_older = max(1, min(168, data.get('hide_older_than_hours', 48)))
        position_size = max(10, min(1000, data.get('position_size_usd', 100)))
        leverage = max(1, min(20, data.get('leverage', 5)))
        score_week_min = max(0, min(100, data.get('score_week_min', 0)))
        score_month_min = max(0, min(100, data.get('score_month_min', 0)))
        max_trades_per_15min = max(1, min(10, data.get('max_trades_per_15min', 3)))

        # Валидация часов
        allowed_hours = data.get('allowed_hours', list(range(24)))
        if not allowed_hours:
            allowed_hours = list(range(24))
        allowed_hours = [h for h in allowed_hours if 0 <= h <= 23]

        # НОВОЕ: Валидация selected_exchanges
        selected_exchanges = data.get('selected_exchanges', [1, 2])
        if not selected_exchanges or not isinstance(selected_exchanges, list):
            return jsonify({
                'status': 'error',
                'message': 'selected_exchanges должен быть непустым списком'
            }), 400

        # Проверяем что все ID существуют
        from database import validate_exchange_ids
        is_valid, valid_ids, invalid_ids = validate_exchange_ids(db, selected_exchanges)
        if not is_valid:
            return jsonify({
                'status': 'error',
                'message': f'Некорректные ID бирж: {invalid_ids}'
            }), 400

        # Сохраняем в БД
        upsert_query = """
            INSERT INTO web.user_signal_filters (
                user_id, hide_younger_than_hours, hide_older_than_hours,
                position_size_usd, leverage, score_week_min, score_month_min,
                allowed_hours, max_trades_per_15min, selected_exchanges
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                hide_younger_than_hours = EXCLUDED.hide_younger_than_hours,
                hide_older_than_hours = EXCLUDED.hide_older_than_hours,
                position_size_usd = EXCLUDED.position_size_usd,
                leverage = EXCLUDED.leverage,
                score_week_min = EXCLUDED.score_week_min,
                score_month_min = EXCLUDED.score_month_min,
                allowed_hours = EXCLUDED.allowed_hours,
                max_trades_per_15min = EXCLUDED.max_trades_per_15min,
                selected_exchanges = EXCLUDED.selected_exchanges,
                updated_at = NOW()
        """

        db.execute_query(upsert_query, (
            current_user.id, hide_younger, hide_older, position_size, leverage,
            score_week_min, score_month_min, allowed_hours, max_trades_per_15min,
            selected_exchanges  # ДОБАВЛЕНО
        ))

        return jsonify({
            'status': 'success',
            'message': 'Фильтры сохранены'
        })
```

#### Тестирование ФАЗА 3:

**Тест 3.1: Запуск приложения**
```bash
# Перезапустить gunicorn
pkill gunicorn
source venv/bin/activate
/home/elcrypto/trading_assistant/venv/bin/gunicorn -c gunicorn_config.py app:app --daemon

# Проверить что приложение запустилось
sleep 2
curl -I http://localhost:5000/

# Ожидаемый результат: HTTP/1.1 302 FOUND (redirect to login)
```

**Тест 3.2: API /api/save_filters**
```bash
# Создать test_api.sh
#!/bin/bash

# Логин и получение cookie
COOKIE=$(curl -s -c - -X POST http://localhost:5000/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"your_password"}' \
  | grep session | awk '{print $7}')

# Тест 1: Сохранение фильтра с обеими биржами
curl -X POST http://localhost:5000/api/save_filters \
  -H 'Content-Type: application/json' \
  -H "Cookie: session=$COOKIE" \
  -d '{
    "selected_exchanges": [1, 2],
    "hide_younger_than_hours": 6,
    "hide_older_than_hours": 48,
    "leverage": 5,
    "position_size_usd": 100
  }'

# Ожидаемый результат: {"status":"success","message":"Фильтры сохранены"}

# Тест 2: Сохранение только Binance
curl -X POST http://localhost:5000/api/save_filters \
  -H 'Content-Type: application/json' \
  -H "Cookie: session=$COOKIE" \
  -d '{
    "selected_exchanges": [1],
    "hide_younger_than_hours": 6,
    "hide_older_than_hours": 48
  }'

# Ожидаемый результат: {"status":"success"}

# Тест 3: Невалидный exchange_id
curl -X POST http://localhost:5000/api/save_filters \
  -H 'Content-Type: application/json' \
  -H "Cookie: session=$COOKIE" \
  -d '{
    "selected_exchanges": [999]
  }'

# Ожидаемый результат: {"status":"error","message":"Некорректные ID бирж: [999]"}
```

**Тест 3.3: Проверка /signal_performance**
```bash
# Проверить что страница загружается
curl -L -b session=$COOKIE http://localhost:5000/signal_performance | grep -o "Эффективность сигналов"

# Ожидаемый результат: "Эффективность сигналов"
```

**Критерии успеха ФАЗА 3:**
- ✅ Приложение запускается без ошибок
- ✅ API /api/save_filters принимает и валидирует selected_exchanges
- ✅ Страница /signal_performance загружается
- ✅ Фильтрация работает на уровне SQL запросов
- ✅ Валидация отклоняет невалидные exchange_id

**Git checkpoint 3:**
```bash
git add app.py
git commit -m "Phase 3: Update API endpoints for exchange filtering

- Add selected_exchanges to /signal_performance route
- Pass exchange_id to process_signal_complete()
- Add exchange filtering to all SQL queries
- Update /api/save_filters to validate and save selected_exchanges
- Add get_exchange_name() helper function
- Pass selected_exchanges to template"
```

---

### 🎨 ФАЗА 4: ОБНОВЛЕНИЕ FRONTEND (HTML + JavaScript)

**Цель:** Добавить UI элементы для выбора бирж

#### Задачи:

**4.1. Добавить блок фильтра по биржам**
- [ ] Создать HTML блок с чекбоксами
- [ ] Добавить кнопку "Применить фильтр"
- [ ] Стилизовать в соответствии с дизайном

**4.2. Добавить JavaScript функцию**
- [ ] Реализовать applyExchangeFilter()
- [ ] Интегрировать с существующими фильтрами
- [ ] Добавить уведомления

**4.3. Добавить колонку "Биржа" в таблицу**
- [ ] Обновить заголовок таблицы
- [ ] Добавить ячейку с badge биржи
- [ ] Стилизовать badges

#### Код изменений:

**Файл: `templates/signal_performance.html`**

**Изменение 1: После строки 31** - Добавить блок фильтра по биржам
```html
    </div>

    <!-- НОВЫЙ БЛОК: Фильтр по биржам -->
    <div class="bg-white rounded-lg shadow-lg p-6 mb-6">
        <h2 class="text-xl font-bold text-gray-800 mb-4">
            <i class="fas fa-building mr-2 text-green-600"></i>
            Фильтр по биржам
        </h2>

        <div class="bg-gray-50 p-4 rounded-lg">
            <p class="text-sm text-gray-700 mb-4">
                Выберите биржи для отображения сигналов. Статистика и параметры рассчитываются только для выбранных бирж.
            </p>

            <div class="flex flex-wrap gap-6 mb-4">
                <label class="flex items-center space-x-3 cursor-pointer group">
                    <input type="checkbox"
                           class="exchange-filter w-5 h-5 text-blue-600 rounded focus:ring-2 focus:ring-blue-500"
                           value="1"
                           {% if 1 in filters.selected_exchanges %}checked{% endif %}>
                    <div class="flex items-center space-x-2">
                        <i class="fab fa-bitcoin text-blue-600 text-xl"></i>
                        <span class="text-sm font-semibold text-gray-800 group-hover:text-blue-600 transition-colors">Binance</span>
                    </div>
                </label>

                <label class="flex items-center space-x-3 cursor-pointer group">
                    <input type="checkbox"
                           class="exchange-filter w-5 h-5 text-yellow-500 rounded focus:ring-2 focus:ring-yellow-400"
                           value="2"
                           {% if 2 in filters.selected_exchanges %}checked{% endif %}>
                    <div class="flex items-center space-x-2">
                        <i class="fas fa-chart-line text-yellow-500 text-xl"></i>
                        <span class="text-sm font-semibold text-gray-800 group-hover:text-yellow-600 transition-colors">Bybit</span>
                    </div>
                </label>
            </div>

            <div class="flex items-center justify-between">
                <div class="text-xs text-gray-600">
                    <i class="fas fa-info-circle mr-1"></i>
                    <span id="selected-exchanges-count">
                        Выбрано бирж: {{ filters.selected_exchanges|length }}
                    </span>
                </div>

                <button onclick="applyExchangeFilter()"
                        class="px-5 py-2 bg-gradient-to-r from-blue-600 to-blue-700 text-white font-medium rounded-lg
                               hover:from-blue-700 hover:to-blue-800 focus:outline-none focus:ring-2 focus:ring-blue-500
                               transition-all duration-200 shadow-md hover:shadow-lg">
                    <i class="fas fa-filter mr-2"></i>
                    Применить фильтр
                </button>
            </div>
        </div>
    </div>

    <!-- Статистика -->
```

**Изменение 2: Строка 472** - Добавить колонку "Биржа" в заголовок таблицы
```html
                <thead class="bg-gray-50">
                <tr>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Пара</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Биржа</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Тип</th>
                    <th class="px-4 py-3 text-left text-xs font-medium text-gray-500 uppercase">Score W/M</th>
```

**Изменение 3: Строка 487** - Добавить ячейку с биржей в строку таблицы
```html
                <tr class="hover:bg-gray-50">
                    <td class="px-4 py-3 text-sm font-medium text-gray-900">{{ signal.pair_symbol }}</td>

                    <!-- НОВАЯ ЯЧЕЙКА: Биржа -->
                    <td class="px-4 py-3 text-sm">
                        <span class="px-2 py-1 text-xs font-medium rounded-full
                            {% if signal.exchange_name == 'Binance' %}bg-blue-100 text-blue-800
                            {% elif signal.exchange_name == 'Bybit' %}bg-yellow-100 text-yellow-800
                            {% else %}bg-gray-100 text-gray-800{% endif %}">
                            {{ signal.exchange_name }}
                        </span>
                    </td>

                    <td class="px-4 py-3 text-sm">
```

**Изменение 4: В блоке <script> (~строка 560)** - Добавить JavaScript функции
```javascript
// Применение фильтра по биржам
function applyExchangeFilter() {
    const selectedExchanges = [];
    document.querySelectorAll('.exchange-filter:checked').forEach(checkbox => {
        selectedExchanges.push(parseInt(checkbox.value));
    });

    if (selectedExchanges.length === 0) {
        showNotification('Выберите хотя бы одну биржу', 'error');
        return;
    }

    // Показываем индикатор загрузки
    const button = event.target.closest('button');
    const originalHTML = button.innerHTML;
    button.disabled = true;
    button.innerHTML = '<i class="fas fa-spinner fa-spin mr-2"></i>Применение...';

    // Собираем все фильтры
    const data = {
        selected_exchanges: selectedExchanges,
        hide_younger_than_hours: parseInt(document.getElementById('hideYounger')?.value || 6),
        hide_older_than_hours: parseInt(document.getElementById('hideOlder')?.value || 48),
        leverage: parseInt(document.getElementById('leverage')?.value || 5),
        position_size_usd: parseFloat(document.getElementById('positionSize')?.value || 100),
        score_week_min: parseInt(document.getElementById('scoreWeek')?.value || 0),
        score_month_min: parseInt(document.getElementById('scoreMonth')?.value || 0),
        max_trades_per_15min: parseInt(document.getElementById('maxTradesPer15Min')?.value || 3),
        allowed_hours: Array.from(document.querySelectorAll('.hour-filter:checked')).map(cb => parseInt(cb.value))
    };

    // Сохраняем фильтр
    fetch('/api/save_filters', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
    })
    .then(response => response.json())
    .then(result => {
        if (result.status === 'success') {
            showNotification(`Фильтр применен: ${getExchangeNames(selectedExchanges).join(', ')}`, 'success');
            // Перезагружаем страницу через 500ms
            setTimeout(() => location.reload(), 500);
        } else {
            showNotification('Ошибка: ' + result.message, 'error');
            button.disabled = false;
            button.innerHTML = originalHTML;
        }
    })
    .catch(error => {
        showNotification('Ошибка сети: ' + error, 'error');
        button.disabled = false;
        button.innerHTML = originalHTML;
    });
}

// Хелпер для получения имен бирж
function getExchangeNames(exchangeIds) {
    const names = {
        1: 'Binance',
        2: 'Bybit',
        3: 'Coinbase'
    };
    return exchangeIds.map(id => names[id] || 'Unknown');
}

// Обновление счетчика выбранных бирж при изменении чекбоксов
document.addEventListener('DOMContentLoaded', function() {
    // ... существующий код ...

    // Слушатель для чекбоксов бирж
    document.querySelectorAll('.exchange-filter').forEach(checkbox => {
        checkbox.addEventListener('change', function() {
            const count = document.querySelectorAll('.exchange-filter:checked').length;
            const countSpan = document.getElementById('selected-exchanges-count');
            if (countSpan) {
                countSpan.textContent = `Выбрано бирж: ${count}`;
            }
        });
    });
});
```

#### Тестирование ФАЗА 4:

**Тест 4.1: UI элементы отображаются**
1. Открыть http://localhost:5000/signal_performance
2. Проверить что блок "Фильтр по биржам" отображается
3. Проверить что чекбоксы работают
4. Проверить что счетчик обновляется при выборе

**Тест 4.2: Таблица содержит колонку "Биржа"**
1. Проверить заголовок таблицы содержит "Биржа"
2. Проверить что в каждой строке есть badge с именем биржи
3. Проверить цвета badges (Binance - синий, Bybit - желтый)

**Тест 4.3: Применение фильтра**
1. Снять чекбокс Bybit, оставить только Binance
2. Нажать "Применить фильтр"
3. Проверить что страница перезагрузилась
4. Проверить что все сигналы только от Binance
5. Проверить что статистика обновилась

**Тест 4.4: Валидация UI**
1. Снять все чекбоксы
2. Нажать "Применить фильтр"
3. Проверить что появилось сообщение "Выберите хотя бы одну биржу"

**Тест 4.5: Сохранение фильтра**
1. Выбрать только Bybit
2. Применить фильтр
3. Обновить страницу (F5)
4. Проверить что чекбокс Bybit остался выбран
5. Проверить что сигналы только от Bybit

**Критерии успеха ФАЗА 4:**
- ✅ UI элементы отображаются корректно
- ✅ Чекбоксы работают
- ✅ Счетчик обновляется
- ✅ Колонка "Биржа" отображается в таблице
- ✅ Badges бирж стилизованы правильно
- ✅ Применение фильтра перезагружает страницу
- ✅ Фильтрация работает
- ✅ Валидация работает
- ✅ Фильтр сохраняется между сессиями

**Git checkpoint 4:**
```bash
git add templates/signal_performance.html
git commit -m "Phase 4: Add exchange filter UI to signal_performance

- Add exchange filter block with Binance/Bybit checkboxes
- Add 'Exchange' column to signals table with styled badges
- Implement applyExchangeFilter() JavaScript function
- Add exchange selection counter
- Add validation for empty selection
- Integrate with existing filter system
- Add loading indicator for filter application"
```

---

### ✅ ФАЗА 5: ИНТЕГРАЦИОННОЕ ТЕСТИРОВАНИЕ

**Цель:** Протестировать все компоненты вместе

#### Тестовые сценарии:

**Сценарий 1: Полный цикл работы фильтра**
1. Логин в систему
2. Переход на /signal_performance
3. Выбор только Binance
4. Применение фильтра
5. Проверка что все сигналы от Binance
6. Проверка статистики (должна учитывать только Binance)
7. Выбор только Bybit
8. Применение фильтра
9. Проверка что все сигналы от Bybit
10. Выбор обеих бирж
11. Применение фильтра
12. Проверка что сигналы от обеих бирж

**Сценарий 2: Интеграция с другими фильтрами**
1. Установить фильтр по возрасту (6-48 часов)
2. Выбрать только Binance
3. Применить
4. Проверить что учитываются оба фильтра
5. Изменить leverage и position_size
6. Применить
7. Проверить что P&L пересчитался

**Сценарий 3: Проверка производительности**
1. Выбрать обе биржи
2. Измерить время загрузки страницы
3. Проверить что < 3 секунд
4. Проверить логи на отсутствие ошибок

**Сценарий 4: Edge cases**
1. Пустая база данных web_signals
2. Только один сигнал в БД
3. Много сигналов (1000+)
4. Невалидный exchange_id в БД
5. Отсутствие прав доступа

#### SQL тесты:

**Тест: Проверка консистентности данных**
```sql
-- Все сигналы должны иметь валидный exchange_id
SELECT COUNT(*)
FROM web.web_signals
WHERE exchange_id IS NULL
   OR exchange_id NOT IN (SELECT id FROM public.exchanges);

-- Ожидаемый результат: 0

-- Проверка что фильтр применяется корректно
SELECT
    e.exchange_name,
    COUNT(*) as signal_count
FROM web.web_signals ws
JOIN public.exchanges e ON e.id = ws.exchange_id
WHERE ws.signal_timestamp >= NOW() - INTERVAL '48 hours'
GROUP BY e.exchange_name;

-- Ожидаемый результат: Binance: N, Bybit: M
```

**Тест: Проверка производительности индексов**
```sql
EXPLAIN (ANALYZE, BUFFERS)
SELECT *
FROM web.web_signals
WHERE exchange_id = ANY(ARRAY[1, 2])
  AND signal_timestamp >= NOW() - INTERVAL '48 hours'
ORDER BY signal_timestamp DESC
LIMIT 100;

-- Ожидаемый результат:
-- Index Scan using idx_web_signals_exchange_timestamp
-- Execution time < 50ms
```

#### Критерии успеха ФАЗА 5:
- ✅ Все тестовые сценарии проходят успешно
- ✅ Нет ошибок в логах приложения
- ✅ Нет ошибок в логах PostgreSQL
- ✅ Время загрузки страницы < 3 секунд
- ✅ SQL запросы используют индексы
- ✅ Edge cases обрабатываются корректно

**Git checkpoint 5:**
```bash
# Создать файл с тестами
git add tests/test_exchange_filter.py
git add tests/test_exchange_filter.sql
git commit -m "Phase 5: Add integration tests for exchange filter

- Add full workflow test scenarios
- Add SQL consistency tests
- Add performance benchmarks
- Add edge case tests
- Document expected results"
```

---

### 📚 ФАЗА 6: ДОКУМЕНТАЦИЯ И ФИНАЛИЗАЦИЯ

**Цель:** Документировать изменения и подготовить к мерджу

#### Задачи:

**6.1. Обновить README**
- [ ] Добавить описание фильтра по биржам
- [ ] Обновить скриншоты (если есть)
- [ ] Добавить примеры использования

**6.2. Создать CHANGELOG**
- [ ] Документировать все изменения
- [ ] Указать breaking changes (если есть)

**6.3. Code review checklist**
- [ ] Все функции имеют docstrings
- [ ] Код соответствует стилю проекта
- [ ] Нет закомментированного кода
- [ ] Нет console.log в production

**6.4. Финальная проверка**
- [ ] Все тесты проходят
- [ ] Нет конфликтов с main
- [ ] Миграция безопасна для production

#### Документы для создания:

**CHANGELOG.md:**
```markdown
# Changelog

## [Unreleased]

### Added
- **Exchange Filter**: Добавлен фильтр по биржам в разделе "Сигналы"
  - Возможность выбора Binance, Bybit или обеих бирж
  - UI с чекбоксами для выбора бирж
  - Колонка "Биржа" в таблице сигналов с цветными badges
  - Сохранение выбора фильтра в настройках пользователя
  - Автоматическая фильтрация статистики по выбранным биржам

### Changed
- `web.user_signal_filters`: добавлена колонка `selected_exchanges INTEGER[]`
- `web.web_signals`: добавлена колонка `exchange_id INTEGER`
- `get_best_scoring_signals_with_backtest_params()`: теперь принимает параметр `selected_exchanges`
- `process_signal_complete()`: теперь сохраняет `exchange_id`
- `/signal_performance`: обновлен для работы с фильтром по биржам
- `/api/save_filters`: добавлена валидация и сохранение `selected_exchanges`

### Database Migration
- Файл миграции: `migrations/001_add_exchange_filter.sql`
- Создает новые колонки с дефолтными значениями
- Заполняет `exchange_id` для существующих записей
- Создает индексы для производительности
- Обратно совместима (можно откатить)

### Performance
- Добавлены индексы:
  - `idx_web_signals_exchange_id`
  - `idx_web_signals_exchange_timestamp`
- Запросы с фильтрацией по биржам используют индексы
- Время загрузки страницы не изменилось

### Breaking Changes
- Нет breaking changes
- Все изменения обратно совместимы
```

**README_EXCHANGE_FILTER.md:**
```markdown
# Фильтр по биржам для signal_performance

## Описание

Фильтр по биржам позволяет отображать сигналы только с выбранных бирж (Binance, Bybit).

## Использование

1. Перейдите на страницу "Сигналы" (/signal_performance)
2. Найдите блок "Фильтр по биржам"
3. Выберите нужные биржи (можно выбрать несколько)
4. Нажмите "Применить фильтр"
5. Страница перезагрузится с отфильтрованными данными

## Технические детали

### База данных

- `web.user_signal_filters.selected_exchanges` - массив ID выбранных бирж
- `web.web_signals.exchange_id` - ID биржи для каждого сигнала

### API

- `GET /signal_performance` - использует selected_exchanges из настроек пользователя
- `POST /api/save_filters` - сохраняет selected_exchanges

### Миграция

Запуск миграции:
```bash
psql -h localhost -U elcrypto -d fox_crypto_new -f migrations/001_add_exchange_filter.sql
```

Откат миграции:
```sql
BEGIN;
ALTER TABLE web.web_signals DROP COLUMN IF EXISTS exchange_id CASCADE;
ALTER TABLE web.user_signal_filters DROP COLUMN IF EXISTS selected_exchanges;
COMMIT;
```

## Тестирование

```bash
# Запустить тесты
python3 tests/test_exchange_filter.py

# Проверить SQL
psql -h localhost -U elcrypto -d fox_crypto_new -f tests/test_exchange_filter.sql
```
```

#### Критерии успеха ФАЗА 6:
- ✅ README обновлен
- ✅ CHANGELOG создан
- ✅ Документация актуальна
- ✅ Code review checklist пройден
- ✅ Нет TODO/FIXME в коде

**Git checkpoint 6:**
```bash
git add CHANGELOG.md README_EXCHANGE_FILTER.md
git commit -m "Phase 6: Add documentation for exchange filter

- Add CHANGELOG with all changes
- Create README_EXCHANGE_FILTER.md with usage guide
- Document migration process
- Add rollback instructions
- Document API changes"
```

---

## ФИНАЛЬНЫЙ ЧЕКЛИСТ ПЕРЕД МЕРДЖЕМ

### Проверки перед мерджем:

- [ ] **База данных:**
  - [ ] Миграция выполнена успешно
  - [ ] Все записи имеют exchange_id
  - [ ] Индексы созданы
  - [ ] Нет NULL значений в критичных полях

- [ ] **Backend:**
  - [ ] Все функции работают с selected_exchanges
  - [ ] Валидация exchange_ids работает
  - [ ] Нет хардкода exchange_id
  - [ ] Логирование добавлено

- [ ] **API:**
  - [ ] /signal_performance работает с фильтром
  - [ ] /api/save_filters валидирует и сохраняет
  - [ ] Ошибки обрабатываются корректно
  - [ ] HTTP коды правильные

- [ ] **Frontend:**
  - [ ] UI элементы отображаются
  - [ ] JavaScript без ошибок
  - [ ] Валидация на клиенте работает
  - [ ] UX интуитивен

- [ ] **Тестирование:**
  - [ ] Все unit тесты проходят
  - [ ] Интеграционные тесты проходят
  - [ ] SQL тесты проходят
  - [ ] Edge cases обработаны

- [ ] **Производительность:**
  - [ ] Запросы используют индексы
  - [ ] Время загрузки < 3 сек
  - [ ] Нет N+1 запросов
  - [ ] Логи чистые

- [ ] **Документация:**
  - [ ] README обновлен
  - [ ] CHANGELOG заполнен
  - [ ] Код документирован
  - [ ] Миграция документирована

- [ ] **Git:**
  - [ ] Все изменения закоммичены
  - [ ] Коммиты имеют понятные сообщения
  - [ ] Нет конфликтов с main
  - [ ] История коммитов чистая

### Команды для финализации:

```bash
# 1. Финальная проверка
git status
git log --oneline

# 2. Rebase на main (если нужно)
git fetch origin
git rebase origin/main

# 3. Прогнать тесты
python3 tests/test_exchange_filter.py
psql -h localhost -U elcrypto -d fox_crypto_new -f tests/test_exchange_filter.sql

# 4. Создать финальный коммит если есть изменения
git add .
git commit -m "Final: Exchange filter ready for merge

Summary:
- Exchange filter fully implemented and tested
- All 6 phases completed
- Documentation added
- Tests passing
- Ready for production

Migration: migrations/001_add_exchange_filter.sql
Breaking changes: None
Backward compatible: Yes"

# 5. Push ветки
git push origin feature/add-exchange-filter

# 6. Создать Pull Request (через GitHub/GitLab)
# Указать:
# - Описание изменений
# - Ссылки на CHANGELOG и README_EXCHANGE_FILTER
# - Checklist пройден
# - Тесты проходят
```

---

## РЕЗЮМЕ ПЛАНА

### Фазы реализации:
1. ✅ **ФАЗА 0:** Подготовка (git, аудит, план)
2. 🗄️ **ФАЗА 1:** Миграция БД (колонки, индексы)
3. 🔧 **ФАЗА 2:** Backend (database.py)
4. 🌐 **ФАЗА 3:** API (app.py)
5. 🎨 **ФАЗА 4:** Frontend (HTML/JS)
6. ✅ **ФАЗА 5:** Интеграционное тестирование
7. 📚 **ФАЗА 6:** Документация

### Время реализации (оценка):
- ФАЗА 1: 30 минут
- ФАЗА 2: 1 час
- ФАЗА 3: 1.5 часа
- ФАЗА 4: 1 час
- ФАЗА 5: 1 час
- ФАЗА 6: 30 минут
- **ИТОГО: ~5.5 часов**

### Риски:
- ⚠️ Низкий: миграция безопасна, изменения изолированы
- ✅ Обратная совместимость: да
- ✅ Rollback: возможен

### Следующий шаг:
**Начать ФАЗУ 1: Миграция базы данных**

---

**ПЛАН ГОТОВ К РЕАЛИЗАЦИИ!**
Все фазы детально проработаны с тестированием на каждом этапе.
