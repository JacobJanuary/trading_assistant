-- Миграция: Создание таблицы scoring_session_summary для сохранения итоговых метрик wave-based scoring
-- Дата: 2025-10-06
-- Цель: Хранение summary метрик симуляции (capital management, drawdown, win rate, etc.)

-- Создание таблицы
CREATE TABLE IF NOT EXISTS web.scoring_session_summary (
    id SERIAL PRIMARY KEY,
    session_id TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    -- Капитал
    initial_capital NUMERIC(15, 2) NOT NULL,
    final_equity NUMERIC(15, 2) NOT NULL,
    min_equity NUMERIC(15, 2) NOT NULL,

    -- PnL
    total_pnl NUMERIC(15, 2) NOT NULL,
    total_pnl_percent NUMERIC(10, 4) NOT NULL,

    -- Метрики позиций
    total_trades INTEGER NOT NULL,
    wins INTEGER NOT NULL,
    losses INTEGER NOT NULL,
    win_rate NUMERIC(10, 2) NOT NULL,
    max_concurrent_positions INTEGER NOT NULL,

    -- Drawdown
    max_drawdown_usd NUMERIC(15, 2) NOT NULL,
    max_drawdown_percent NUMERIC(10, 4) NOT NULL,

    -- Комиссии
    total_commission_paid NUMERIC(15, 2) NOT NULL,

    -- Статистика обработки
    total_signals_processed INTEGER NOT NULL,
    trades_opened INTEGER NOT NULL,
    trades_closed INTEGER NOT NULL,
    skipped_no_capital INTEGER NOT NULL,
    skipped_duplicate INTEGER NOT NULL,
    skipped_wave_limit INTEGER NOT NULL,

    -- Параметры симуляции
    position_size NUMERIC(15, 2) NOT NULL,
    leverage INTEGER NOT NULL,
    tp_percent NUMERIC(10, 2) NOT NULL,
    sl_percent NUMERIC(10, 2) NOT NULL,
    use_trailing_stop BOOLEAN NOT NULL,
    trailing_distance_pct NUMERIC(10, 2),
    trailing_activation_pct NUMERIC(10, 2)
);

-- Индексы для быстрого поиска
CREATE INDEX IF NOT EXISTS idx_scoring_session_summary_session_id
    ON web.scoring_session_summary(session_id);

CREATE INDEX IF NOT EXISTS idx_scoring_session_summary_user_id
    ON web.scoring_session_summary(user_id);

CREATE INDEX IF NOT EXISTS idx_scoring_session_summary_created_at
    ON web.scoring_session_summary(created_at DESC);

-- Комментарии
COMMENT ON TABLE web.scoring_session_summary IS
    'Итоговые метрики wave-based scoring analysis с управлением капиталом';

COMMENT ON COLUMN web.scoring_session_summary.initial_capital IS
    'Начальный капитал симуляции';

COMMENT ON COLUMN web.scoring_session_summary.final_equity IS
    'Итоговый equity после всех сделок';

COMMENT ON COLUMN web.scoring_session_summary.min_equity IS
    'Минимальный equity за период (с учетом floating PnL)';

COMMENT ON COLUMN web.scoring_session_summary.max_drawdown_usd IS
    'Максимальная просадка в долларах';

COMMENT ON COLUMN web.scoring_session_summary.max_drawdown_percent IS
    'Максимальная просадка в процентах';

COMMENT ON COLUMN web.scoring_session_summary.max_concurrent_positions IS
    'Максимальное количество одновременно открытых позиций';

COMMENT ON COLUMN web.scoring_session_summary.skipped_wave_limit IS
    'Количество сигналов пропущенных из-за лимита сделок на волну';
