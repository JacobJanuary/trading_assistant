-- Добавление колонок для фильтров скоринга в таблицу user_signal_filters
ALTER TABLE web.user_signal_filters 
ADD COLUMN IF NOT EXISTS score_week_min INTEGER DEFAULT 0,
ADD COLUMN IF NOT EXISTS score_month_min INTEGER DEFAULT 0;

-- Комментарии к колонкам
COMMENT ON COLUMN web.user_signal_filters.score_week_min IS 'Минимальный Score Week для фильтрации сигналов';
COMMENT ON COLUMN web.user_signal_filters.score_month_min IS 'Минимальный Score Month для фильтрации сигналов';

-- Обновляем существующие записи со значениями по умолчанию
UPDATE web.user_signal_filters 
SET score_week_min = 0, score_month_min = 0 
WHERE score_week_min IS NULL OR score_month_min IS NULL;