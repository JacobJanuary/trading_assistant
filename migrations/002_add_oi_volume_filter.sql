-- ============================================================================
-- MIGRATION: Add OI/Volume Filter Support
-- Date: 2025-11-12
-- Description: Добавляет фильтр по Open Interest и Volume для сигналов
-- Author: Claude Code
-- ============================================================================

BEGIN;

-- 1. Добавляем колонку enable_oi_volume_filter в user_signal_filters
ALTER TABLE web.user_signal_filters
ADD COLUMN IF NOT EXISTS enable_oi_volume_filter BOOLEAN DEFAULT FALSE;

COMMENT ON COLUMN web.user_signal_filters.enable_oi_volume_filter IS
'Включить фильтрацию по OI/Volume. Исключает сигналы где:
 - open_interest < 500,000 ИЛИ
 - mark_price * volume < 10,000
Данные берутся из fas_v2.market_data_aggregated (timeframe=15m)';

-- 2. Создаем индекс на market_data_aggregated для производительности
-- (опционально, т.к. текущая производительность уже отличная)
CREATE INDEX IF NOT EXISTS idx_market_data_aggregated_lookup
ON fas_v2.market_data_aggregated(trading_pair_id, timestamp, timeframe)
WHERE timeframe = '15m';

COMMENT ON INDEX fas_v2.idx_market_data_aggregated_lookup IS
'Индекс для быстрого поиска market data по trading_pair_id и timestamp для OI/Volume фильтра';

-- 3. Проверяем результаты миграции
DO $$
DECLARE
    column_exists BOOLEAN;
    index_exists BOOLEAN;
BEGIN
    -- Проверяем колонку
    SELECT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = 'web'
          AND table_name = 'user_signal_filters'
          AND column_name = 'enable_oi_volume_filter'
    ) INTO column_exists;

    -- Проверяем индекс
    SELECT EXISTS (
        SELECT 1
        FROM pg_indexes
        WHERE schemaname = 'fas_v2'
          AND tablename = 'market_data_aggregated'
          AND indexname = 'idx_market_data_aggregated_lookup'
    ) INTO index_exists;

    RAISE NOTICE '========================================';
    RAISE NOTICE 'Migration Results:';
    RAISE NOTICE '========================================';
    RAISE NOTICE 'Column enable_oi_volume_filter: %', CASE WHEN column_exists THEN '✓ EXISTS' ELSE '✗ MISSING' END;
    RAISE NOTICE 'Index idx_market_data_aggregated_lookup: %', CASE WHEN index_exists THEN '✓ EXISTS' ELSE '✗ MISSING' END;
    RAISE NOTICE '========================================';

    IF column_exists AND index_exists THEN
        RAISE NOTICE 'SUCCESS: Migration completed successfully!';
    ELSE
        RAISE WARNING 'WARNING: Migration incomplete!';
    END IF;
END $$;

-- 4. Проверяем структуру новой колонки
SELECT
    table_name,
    column_name,
    data_type,
    column_default,
    is_nullable
FROM information_schema.columns
WHERE table_schema = 'web'
  AND table_name = 'user_signal_filters'
  AND column_name = 'enable_oi_volume_filter';

COMMIT;

-- ============================================================================
-- ROLLBACK SCRIPT (если нужно откатить миграцию)
-- ============================================================================
-- BEGIN;
-- ALTER TABLE web.user_signal_filters DROP COLUMN IF EXISTS enable_oi_volume_filter;
-- DROP INDEX IF EXISTS fas_v2.idx_market_data_aggregated_lookup;
-- COMMIT;
