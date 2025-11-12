-- ============================================================================
-- MIGRATION: Add Exchange Filter Support
-- Date: 2025-11-12
-- Description: Добавляет возможность фильтрации сигналов по биржам
-- Author: Claude Code
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

-- Удаляем старый constraint если существует
ALTER TABLE web.web_signals
DROP CONSTRAINT IF EXISTS fk_web_signals_exchange;

-- Добавляем foreign key (без NOT NULL пока не заполним данные)
ALTER TABLE web.web_signals
ADD CONSTRAINT fk_web_signals_exchange
FOREIGN KEY (exchange_id) REFERENCES public.exchanges(id);

-- Создаем индекс для производительности
CREATE INDEX IF NOT EXISTS idx_web_signals_exchange_id
ON web.web_signals(exchange_id);

-- Создаем составной индекс для частых запросов (exchange + timestamp)
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

    RAISE NOTICE '========================================';
    RAISE NOTICE 'Migration Results:';
    RAISE NOTICE '========================================';
    RAISE NOTICE 'Total signals: %', total_signals;
    RAISE NOTICE 'With exchange_id: %', signals_with_exchange;
    RAISE NOTICE 'Without exchange_id: %', signals_without_exchange;
    RAISE NOTICE '========================================';

    IF signals_without_exchange > 0 THEN
        RAISE WARNING 'Found % signals without exchange_id! Check pair_symbol mappings.', signals_without_exchange;
    ELSE
        RAISE NOTICE 'SUCCESS: All signals have exchange_id!';
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
    column_default,
    is_nullable
FROM information_schema.columns
WHERE table_schema = 'web'
  AND table_name = 'user_signal_filters'
  AND column_name = 'selected_exchanges'
UNION ALL
SELECT
    'web_signals' as table_name,
    column_name,
    data_type,
    column_default,
    is_nullable
FROM information_schema.columns
WHERE table_schema = 'web'
  AND table_name = 'web_signals'
  AND column_name = 'exchange_id';

-- 7. Проверяем индексы
SELECT
    schemaname,
    tablename,
    indexname,
    indexdef
FROM pg_indexes
WHERE tablename = 'web_signals'
  AND indexname LIKE '%exchange%';

COMMIT;

-- ============================================================================
-- ROLLBACK SCRIPT (если нужно откатить миграцию)
-- ============================================================================
-- BEGIN;
-- ALTER TABLE web.web_signals DROP COLUMN IF EXISTS exchange_id CASCADE;
-- ALTER TABLE web.user_signal_filters DROP COLUMN IF EXISTS selected_exchanges;
-- COMMIT;
