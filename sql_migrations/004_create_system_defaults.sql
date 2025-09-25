-- Миграция 004: Создание таблицы system_defaults для хранения системных настроек
-- Дата: 2025-09-25

-- Создаем таблицу для системных настроек если её еще нет
CREATE TABLE IF NOT EXISTS web.system_defaults (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT NOT NULL,
    value_type VARCHAR(20) DEFAULT 'string', -- string, integer, float, boolean, json
    description TEXT,
    category VARCHAR(50), -- trading, system, limits, analysis
    is_editable BOOLEAN DEFAULT TRUE, -- можно ли редактировать через UI
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Создаем индекс для быстрого поиска по ключу
CREATE INDEX IF NOT EXISTS idx_system_defaults_key ON web.system_defaults(key);
CREATE INDEX IF NOT EXISTS idx_system_defaults_category ON web.system_defaults(category);

-- Функция для автоматического обновления updated_at
CREATE OR REPLACE FUNCTION update_system_defaults_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Триггер для обновления updated_at
DROP TRIGGER IF EXISTS update_system_defaults_updated_at ON web.system_defaults;
CREATE TRIGGER update_system_defaults_updated_at
BEFORE UPDATE ON web.system_defaults
FOR EACH ROW
EXECUTE FUNCTION update_system_defaults_updated_at();

-- Вставляем дефолтные значения (если они еще не существуют)
INSERT INTO web.system_defaults (key, value, value_type, description, category, is_editable)
VALUES 
    -- Торговые параметры
    ('default_position_size', '100', 'float', 'Размер позиции по умолчанию в USD', 'trading', true),
    ('default_leverage', '5', 'integer', 'Кредитное плечо по умолчанию', 'trading', true),
    ('default_stop_loss_percent', '3.0', 'float', 'Stop Loss по умолчанию в процентах', 'trading', true),
    ('default_take_profit_percent', '4.0', 'float', 'Take Profit по умолчанию в процентах', 'trading', true),
    ('default_trailing_distance_pct', '2.0', 'float', 'Дистанция Trailing Stop по умолчанию в процентах', 'trading', true),
    ('default_trailing_activation_pct', '1.0', 'float', 'Активация Trailing Stop по умолчанию в процентах', 'trading', true),
    ('default_use_trailing_stop', 'false', 'boolean', 'Использовать Trailing Stop по умолчанию', 'trading', true),
    
    -- Временные фильтры
    ('default_hide_younger_hours', '6', 'integer', 'Скрывать сигналы моложе N часов', 'trading', true),
    ('default_hide_older_hours', '48', 'integer', 'Скрывать сигналы старше N часов', 'trading', true),
    
    -- Score фильтры
    ('default_score_week_min', '0', 'float', 'Минимальный недельный score', 'trading', true),
    ('default_score_month_min', '0', 'float', 'Минимальный месячный score', 'trading', true),
    
    -- Лимиты
    ('default_min_value_usd', '10000', 'float', 'Минимальный объем сделки в USD', 'limits', true),
    ('default_max_trades_per_15min', '3', 'integer', 'Максимум сделок за 15 минут', 'limits', true),
    
    -- Лимиты позиций
    ('min_position_size', '10', 'float', 'Минимальный размер позиции', 'limits', false),
    ('max_position_size', '10000', 'float', 'Максимальный размер позиции', 'limits', false),
    ('min_leverage', '1', 'integer', 'Минимальное кредитное плечо', 'limits', false),
    ('max_leverage', '20', 'integer', 'Максимальное кредитное плечо', 'limits', false),
    
    -- Лимиты SL/TP
    ('min_stop_loss', '0.5', 'float', 'Минимальный Stop Loss в процентах', 'limits', false),
    ('max_stop_loss', '20', 'float', 'Максимальный Stop Loss в процентах', 'limits', false),
    ('min_take_profit', '0.5', 'float', 'Минимальный Take Profit в процентах', 'limits', false),
    ('max_take_profit', '50', 'float', 'Максимальный Take Profit в процентах', 'limits', false),
    
    -- Лимиты Trailing Stop
    ('min_trailing_distance', '0.1', 'float', 'Минимальная дистанция Trailing в процентах', 'limits', false),
    ('max_trailing_distance', '10', 'float', 'Максимальная дистанция Trailing в процентах', 'limits', false),
    ('min_trailing_activation', '0.1', 'float', 'Минимальная активация Trailing в процентах', 'limits', false),
    ('max_trailing_activation', '10', 'float', 'Максимальная активация Trailing в процентах', 'limits', false),
    
    -- Параметры анализа
    ('efficiency_analysis_max_combinations', '1000', 'integer', 'Максимум комбинаций для анализа эффективности', 'analysis', true),
    ('efficiency_analysis_timeout', '300', 'integer', 'Таймаут анализа эффективности в секундах', 'analysis', true),
    ('scoring_analysis_default_days_back', '30', 'integer', 'Дней назад для scoring анализа', 'analysis', true),
    ('scoring_analysis_batch_size', '50', 'integer', 'Размер пакета для scoring анализа', 'analysis', true),
    
    -- Системные параметры
    ('session_lifetime', '3600', 'integer', 'Время жизни сессии в секундах', 'system', false),
    ('max_login_attempts', '5', 'integer', 'Максимум попыток входа', 'system', false),
    ('login_attempt_window', '300', 'integer', 'Окно для подсчета попыток входа в секундах', 'system', false)
ON CONFLICT (key) DO NOTHING;

-- Комментарии к таблице
COMMENT ON TABLE web.system_defaults IS 'Системные настройки и дефолтные значения приложения';
COMMENT ON COLUMN web.system_defaults.key IS 'Уникальный ключ настройки';
COMMENT ON COLUMN web.system_defaults.value IS 'Значение настройки (хранится как текст)';
COMMENT ON COLUMN web.system_defaults.value_type IS 'Тип значения для правильного преобразования';
COMMENT ON COLUMN web.system_defaults.description IS 'Описание настройки для пользователей';
COMMENT ON COLUMN web.system_defaults.category IS 'Категория настройки для группировки';
COMMENT ON COLUMN web.system_defaults.is_editable IS 'Можно ли редактировать через интерфейс администратора';

-- Вывод результата
SELECT COUNT(*) as total_settings, 
       COUNT(CASE WHEN category = 'trading' THEN 1 END) as trading_settings,
       COUNT(CASE WHEN category = 'limits' THEN 1 END) as limit_settings,
       COUNT(CASE WHEN category = 'analysis' THEN 1 END) as analysis_settings,
       COUNT(CASE WHEN category = 'system' THEN 1 END) as system_settings
FROM web.system_defaults;