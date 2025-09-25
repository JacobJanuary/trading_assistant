-- Добавление колонки для фильтра по часам создания сигнала
ALTER TABLE web.user_signal_filters 
ADD COLUMN IF NOT EXISTS allowed_hours INTEGER[] DEFAULT ARRAY[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23];

-- Комментарий к колонке
COMMENT ON COLUMN web.user_signal_filters.allowed_hours IS 'Массив часов (0-23), в которые разрешены сигналы';

-- Обновляем существующие записи со всеми часами по умолчанию
UPDATE web.user_signal_filters 
SET allowed_hours = ARRAY[0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23]
WHERE allowed_hours IS NULL;