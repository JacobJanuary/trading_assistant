# КРИТИЧЕСКАЯ ПРОБЛЕМА: Сигналы не отображаются после миграции на public.candles

## Дата расследования
2025-11-06

## Статус
🔴 CRITICAL - Система полностью не работает с public.candles

## Executive Summary
После миграции на `public.candles` (USE_PUBLIC_CANDLES=true) система НЕ МОЖЕТ получать данные свечей, что приводит к отсутствию сигналов. Проблема вызвана **полностью неправильным маппингом колонок** в функции `get_candle_table_info()`.

---

## ROOT CAUSE

### Проблема #1: Неправильные названия колонок цен

**Предположение в коде (НЕВЕРНОЕ):**
```python
# database.py, строки 59-64
if Config.USE_PUBLIC_CANDLES:
    return "public.candles", {
        'open': 'open AS open_price',      # ❌ ОШИБКА!
        'high': 'high AS high_price',      # ❌ ОШИБКА!
        'low': 'low AS low_price',         # ❌ ОШИБКА!
        'close': 'close AS close_price',   # ❌ ОШИБКА!
```

**Реальная структура public.candles:**
```sql
id                   bigint
trading_pair_id      integer
interval_id          integer              -- НЕ timeframe!
open_time            bigint               -- НЕ timestamp!
open_price           numeric              -- УЖЕ ЕСТЬ _price суффикс
high_price           numeric              -- УЖЕ ЕСТЬ _price суффикс
low_price            numeric              -- УЖЕ ЕСТЬ _price суффикс
close_price          numeric              -- УЖЕ ЕСТЬ _price суффикс
volume               numeric
quote_asset_volume   numeric
...
```

**Что происходит:**
Код пытается сделать `SELECT open AS open_price`, но в таблице НЕТ колонки `open`!
Колонка называется `open_price` (как в старой таблице fas_v2.market_data_aggregated).

---

### Проблема #2: Отсутствует колонка timestamp

**Предположение в коде (НЕВЕРНОЕ):**
```python
'timestamp': 'timestamp',  # ❌ В public.candles НЕТ такой колонки!
```

**Реальность:**
- В `public.candles` используется `open_time` (BIGINT, Unix timestamp в миллисекундах)
- Нужно конвертировать: `to_timestamp(open_time / 1000)`

---

### Проблема #3: Отсутствует колонка timeframe

**Предположение в коде (НЕВЕРНОЕ):**
```python
'timeframe': 'timeframe'  # ❌ В public.candles НЕТ такой колонки!
```

**Реальность:**
- В `public.candles` используется `interval_id` (INTEGER, foreign key)
- Связь через таблицу `public.intervals`:

```sql
SELECT * FROM public.intervals;
 id | name
----+-------
  1 | 5m
  2 | 15m
  3 | 1h
  4 | 4h
  5 | 24h
```

- Для фильтрации по timeframe='5m' нужно:
  - Делать JOIN с public.intervals
  - Или использовать WHERE interval_id = 1

---

## Сгенерированный SQL (НЕПРАВИЛЬНЫЙ)

**Текущий код генерирует:**
```sql
SELECT open AS open_price as entry_price  -- ❌ SYNTAX ERROR!
FROM public.candles
WHERE trading_pair_id = %s
    AND timeframe = '5m'                  -- ❌ Колонки НЕТ!
    AND timestamp >= %s - INTERVAL '5 minutes'  -- ❌ Колонки НЕТ!
```

**Ошибки:**
1. `open` - колонка не существует
2. `timeframe` - колонка не существует
3. `timestamp` - колонка не существует
4. `open AS open_price as entry_price` - двойной AS (syntax error)

---

## Статистика данных

### public.candles (последние 24 часа)
- **Всего записей:** 490,053
- **Уникальных пар:** 1,196
- **Уникальных интервалов:** 4
- **Период:** 2025-11-05 22:25:00 → 2025-11-06 22:15:00

### Распределение по interval_id
```
interval_id=1 (5m):   343,016 records, 1,196 pairs
interval_id=2 (15m):  113,567 records, 1,196 pairs
interval_id=3 (1h):    27,496 records, 1,196 pairs
interval_id=4 (4h):     5,980 records, 1,196 pairs
```

### web.web_signals
- **Всего сигналов:** 0 ❌
- **За последние 24 часа:** 0 ❌

**Вывод:** Сигналы НЕ создаются, потому что запросы ПАДАЮТ с ошибками SQL.

---

## Сравнение структур таблиц

### fas_v2.market_data_aggregated (LEGACY, работает)
```sql
trading_pair_id      integer              ✅
timeframe            USER-DEFINED         ✅ Enum: '5m', '15m', etc
timestamp            timestamp with time zone  ✅
open_price           numeric              ✅
high_price           numeric              ✅
low_price            numeric              ✅
close_price          numeric              ✅
```

### public.candles (NEW, НЕ работает)
```sql
trading_pair_id      integer              ✅
interval_id          integer              ❌ Вместо timeframe (FK -> intervals.id)
open_time            bigint               ❌ Вместо timestamp (Unix ms)
open_price           numeric              ✅
high_price           numeric              ✅
low_price            numeric              ✅
close_price          numeric              ✅
```

---

## ПРАВИЛЬНЫЙ SQL для public.candles

### Вариант 1: С JOIN (более читаемый)
```sql
SELECT
    c.open_price as entry_price
FROM public.candles c
JOIN public.intervals i ON c.interval_id = i.id
WHERE c.trading_pair_id = %s
    AND i.name = '5m'
    AND to_timestamp(c.open_time / 1000) >= %s - INTERVAL '5 minutes'
    AND to_timestamp(c.open_time / 1000) <= %s + INTERVAL '5 minutes'
ORDER BY ABS(EXTRACT(EPOCH FROM (to_timestamp(c.open_time / 1000) - %s))) ASC
LIMIT 1
```

### Вариант 2: Без JOIN (быстрее)
```sql
SELECT
    open_price as entry_price
FROM public.candles
WHERE trading_pair_id = %s
    AND interval_id = 1  -- 5m
    AND to_timestamp(open_time / 1000) >= %s - INTERVAL '5 minutes'
    AND to_timestamp(open_time / 1000) <= %s + INTERVAL '5 minutes'
ORDER BY ABS(EXTRACT(EPOCH FROM (to_timestamp(open_time / 1000) - %s))) ASC
LIMIT 1
```

---

## Тестирование правильного запроса

**Запрос:**
```sql
SELECT
    to_timestamp(c.open_time / 1000) as timestamp,
    c.open_price,
    c.high_price,
    c.low_price,
    c.close_price
FROM public.candles c
JOIN public.intervals i ON c.interval_id = i.id
WHERE c.trading_pair_id = 2115  -- BTC
    AND i.name = '5m'
    AND to_timestamp(c.open_time / 1000) >= NOW() - INTERVAL '1 hour'
ORDER BY c.open_time DESC
LIMIT 5
```

**Результат (SUCCESS!):**
```
2025-11-06 22:35:00 | O:101203.9 H:101203.9 L:101025.3 C:101084.3
2025-11-06 22:30:00 | O:101092.9 H:101246.3 L:101037.0 C:101203.9
2025-11-06 22:25:00 | O:101256.1 H:101256.1 L:101003.1 C:101092.9
2025-11-06 22:20:00 | O:101153.1 H:101278.9 L:101112.4 C:101256.0
2025-11-06 22:15:00 | O:100950.8 H:101153.1 L:100950.7 C:101153.1
```

---

## Затронутые функции

Все функции используют `get_candle_table_info()` и сломаны:

1. **build_entry_price_query()** (database.py:82)
   - Используется в: process_signal(), process_signal_with_wave_scoring(), score_signals_batch()

2. **build_entry_price_fallback_query()** (database.py:110)
   - Fallback для build_entry_price_query()

3. **build_candle_history_query()** (database.py:138)
   - Используется для получения истории свечей

**Вызовы:**
- `database.py:1233` - process_signal()
- `database.py:1964` - process_signal_with_wave_scoring()
- `database.py:2671` - score_signals_batch()
- `database.py:3215` - simulation в wave-based scoring

---

## FIX REQUIRED

### Вариант A: Исправить get_candle_table_info() - WITH JOIN

```python
def get_candle_table_info():
    if Config.USE_PUBLIC_CANDLES:
        # public.candles требует JOIN с intervals
        return "public.candles c JOIN public.intervals i ON c.interval_id = i.id", {
            'open': 'c.open_price',
            'high': 'c.high_price',
            'low': 'c.low_price',
            'close': 'c.close_price',
            'timestamp': 'to_timestamp(c.open_time / 1000)',
            'trading_pair_id': 'c.trading_pair_id',
            'timeframe': 'i.name'  # Используем intervals.name
        }
    else:
        # fas_v2.market_data_aggregated - legacy
        return "fas_v2.market_data_aggregated", {
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'close': 'close_price',
            'timestamp': 'timestamp',
            'trading_pair_id': 'trading_pair_id',
            'timeframe': 'timeframe'
        }
```

### Вариант B: Исправить get_candle_table_info() - WITHOUT JOIN (FASTER!)

```python
def get_candle_table_info():
    if Config.USE_PUBLIC_CANDLES:
        # public.candles БЕЗ JOIN - используем interval_id напрямую
        return "public.candles", {
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'close': 'close_price',
            'timestamp': 'to_timestamp(open_time / 1000)',
            'trading_pair_id': 'trading_pair_id',
            'timeframe': 'interval_id'  # ⚠️ Нужно изменить сравнение '5m' -> 1
        }
    else:
        # fas_v2.market_data_aggregated - legacy
        return "fas_v2.market_data_aggregated", {
            'open': 'open_price',
            'high': 'high_price',
            'low': 'low_price',
            'close': 'close_price',
            'timestamp': 'timestamp',
            'trading_pair_id': 'trading_pair_id',
            'timeframe': 'timeframe'
        }
```

**Для варианта B нужно также:**
- Создать helper function `get_interval_id(timeframe_name)`
- Заменить все `AND timeframe = '5m'` на `AND interval_id = get_interval_id('5m')`

---

## Рекомендация

**Используйте ВАРИАНТ A (с JOIN):**

**Преимущества:**
- Минимальные изменения в коде
- Сохраняется совместимость со строковыми timeframe ('5m', '15m')
- Понятнее для отладки

**Недостатки:**
- JOIN добавляет небольшой overhead
- Но на практике с индексами это незаметно

**Альтернатива (ВАРИАНТ B):**
- Быстрее (без JOIN)
- Но требует больше изменений (маппинг '5m' -> 1 во всех запросах)

---

## Следующие шаги

1. ✅ **Немедленно исправить** `get_candle_table_info()`
2. ✅ Протестировать все 3 query builder функции
3. ✅ Проверить создание сигналов
4. ⚠️ Добавить unit-тесты для обоих источников данных
5. ⚠️ Рассмотреть создание индекса: `CREATE INDEX idx_candles_lookup ON public.candles(trading_pair_id, interval_id, open_time)`

---

## Заключение

Система **полностью сломана** при USE_PUBLIC_CANDLES=true из-за неправильного маппинга колонок.

**Причина:**
Предполагалось, что public.candles имеет колонки `open`, `high`, `low`, `close`, `timestamp`, `timeframe`.
Реально: `open_price`, `high_price`, `low_price`, `close_price`, `open_time`, `interval_id`.

**Решение:**
Исправить функцию `get_candle_table_info()` с учетом реальной структуры таблицы.

---

## Тестовые данные

### Проверка данных в public.candles
```sql
-- Проверить что данные есть
SELECT
    COUNT(*) as total,
    COUNT(DISTINCT trading_pair_id) as pairs,
    to_timestamp(MIN(open_time) / 1000) as earliest,
    to_timestamp(MAX(open_time) / 1000) as latest
FROM public.candles
WHERE to_timestamp(open_time / 1000) >= NOW() - INTERVAL '24 hours';

-- Результат:
-- total: 490053
-- pairs: 1196
-- earliest: 2025-11-05 22:25:00
-- latest: 2025-11-06 22:15:00
```

### Проверка intervals mapping
```sql
SELECT id, name FROM public.intervals ORDER BY id;

-- Результат:
-- 1 | 5m
-- 2 | 15m
-- 3 | 1h
-- 4 | 4h
-- 5 | 24h
```

**Данные ЕСТЬ. Запросы ПАДАЮТ из-за неправильного SQL.**
