"""
Централизованная конфигурация приложения Trading Assistant
Загружает настройки из переменных окружения с дефолтными значениями
"""
import os
from dotenv import load_dotenv

# Загрузка переменных окружения из .env файла
load_dotenv()

class Config:
    """Основной класс конфигурации"""
    
    # ============================================
    # БАЗА ДАННЫХ
    # ============================================
    
    # Основные параметры подключения
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = int(os.getenv('DB_PORT', 5432))
    DB_NAME = os.getenv('DB_NAME', 'trading_assistant')
    DB_USER = os.getenv('DB_USER', 'postgres')
    DB_PASSWORD = os.getenv('DB_PASSWORD')  # Может быть None если используется .pgpass
    
    # Параметры пула соединений
    DB_POOL_MIN_SIZE = int(os.getenv('DB_POOL_MIN_SIZE', 1))  # Уменьшено до 1 - минимум idle соединений
    DB_POOL_MAX_SIZE = int(os.getenv('DB_POOL_MAX_SIZE', 5))  # Уменьшено до 5 - меньше соединений
    DB_POOL_TIMEOUT = float(os.getenv('DB_POOL_TIMEOUT', 30.0))
    DB_POOL_MAX_IDLE = float(os.getenv('DB_POOL_MAX_IDLE', 60.0))  # Уменьшено до 60 сек - закрываем idle очень быстро
    DB_POOL_MAX_LIFETIME = float(os.getenv('DB_POOL_MAX_LIFETIME', 300.0))  # Уменьшено до 300 (5 мин) - частое обновление
    DB_POOL_MAX_WAITING = int(os.getenv('DB_POOL_MAX_WAITING', 20))
    
    # Параметры соединения
    DB_CONNECT_TIMEOUT = int(os.getenv('DB_CONNECT_TIMEOUT', 10))
    DB_KEEPALIVES = int(os.getenv('DB_KEEPALIVES', 1))
    DB_KEEPALIVES_IDLE = int(os.getenv('DB_KEEPALIVES_IDLE', 10))  # Уменьшено с 30 до 10 - быстрее обнаружение разрывов
    DB_KEEPALIVES_INTERVAL = int(os.getenv('DB_KEEPALIVES_INTERVAL', 2))  # Уменьшено с 5 до 2 - чаще проверка
    DB_KEEPALIVES_COUNT = int(os.getenv('DB_KEEPALIVES_COUNT', 5))
    DB_TCP_USER_TIMEOUT = int(os.getenv('DB_TCP_USER_TIMEOUT', 30000))  # Уменьшено с 60000 до 30000 - быстрее таймаут
    
    # Параметры повторных попыток
    DB_MAX_RETRIES = int(os.getenv('DB_MAX_RETRIES', 3))
    DB_MAX_ERRORS_BEFORE_REINIT = int(os.getenv('DB_MAX_ERRORS_BEFORE_REINIT', 5))
    DB_ERROR_RESET_INTERVAL = int(os.getenv('DB_ERROR_RESET_INTERVAL', 60))
    
    # ============================================
    # ПРИЛОЖЕНИЕ
    # ============================================
    
    # Flask настройки
    SECRET_KEY = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
    FLASK_ENV = os.getenv('FLASK_ENV', 'development')
    FLASK_DEBUG = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    DEBUG_AUTH = os.getenv('DEBUG_AUTH', 'False').lower() == 'true'
    
    # Сервер
    HOST = os.getenv('HOST', '0.0.0.0')
    PORT = int(os.getenv('PORT', 5000))
    
    # ============================================
    # ТОРГОВЫЕ ПАРАМЕТРЫ ПО УМОЛЧАНИЮ
    # ============================================
    
    # Основные торговые параметры
    DEFAULT_POSITION_SIZE = float(os.getenv('DEFAULT_POSITION_SIZE', 100.0))
    DEFAULT_LEVERAGE = int(os.getenv('DEFAULT_LEVERAGE', 5))
    DEFAULT_STOP_LOSS_PERCENT = float(os.getenv('DEFAULT_STOP_LOSS_PERCENT', 3.0))
    DEFAULT_TAKE_PROFIT_PERCENT = float(os.getenv('DEFAULT_TAKE_PROFIT_PERCENT', 4.0))

    # Комиссии (0.06% = 0.0006)
    DEFAULT_COMMISSION_RATE = float(os.getenv('DEFAULT_COMMISSION_RATE', 0.0006))
    
    # Trailing Stop параметры
    DEFAULT_TRAILING_DISTANCE_PCT = float(os.getenv('DEFAULT_TRAILING_DISTANCE_PCT', 2.0))
    DEFAULT_TRAILING_ACTIVATION_PCT = float(os.getenv('DEFAULT_TRAILING_ACTIVATION_PCT', 1.0))
    DEFAULT_USE_TRAILING_STOP = os.getenv('DEFAULT_USE_TRAILING_STOP', 'False').lower() == 'true'
    
    # Временные фильтры
    DEFAULT_HIDE_YOUNGER_HOURS = int(os.getenv('DEFAULT_HIDE_YOUNGER_HOURS', 6))
    DEFAULT_HIDE_OLDER_HOURS = int(os.getenv('DEFAULT_HIDE_OLDER_HOURS', 48))
    
    # Score фильтры
    DEFAULT_SCORE_WEEK_MIN = float(os.getenv('DEFAULT_SCORE_WEEK_MIN', 0))
    DEFAULT_SCORE_MONTH_MIN = float(os.getenv('DEFAULT_SCORE_MONTH_MIN', 0))
    
    # Лимиты
    DEFAULT_MIN_VALUE_USD = float(os.getenv('DEFAULT_MIN_VALUE_USD', 10000))
    DEFAULT_MAX_TRADES_PER_15MIN = int(os.getenv('DEFAULT_MAX_TRADES_PER_15MIN', 3))
    
    # Разрешенные часы торговли
    DEFAULT_ALLOWED_HOURS = os.getenv('DEFAULT_ALLOWED_HOURS', ','.join(map(str, range(24))))
    
    # ============================================
    # ЛИМИТЫ И ВАЛИДАЦИЯ
    # ============================================
    
    # Position size лимиты
    MIN_POSITION_SIZE = float(os.getenv('MIN_POSITION_SIZE', 10))
    MAX_POSITION_SIZE = float(os.getenv('MAX_POSITION_SIZE', 10000))
    
    # Leverage лимиты
    MIN_LEVERAGE = int(os.getenv('MIN_LEVERAGE', 1))
    MAX_LEVERAGE = int(os.getenv('MAX_LEVERAGE', 20))
    
    # Stop loss/Take profit лимиты
    MIN_STOP_LOSS = float(os.getenv('MIN_STOP_LOSS', 0.5))
    MAX_STOP_LOSS = float(os.getenv('MAX_STOP_LOSS', 20))
    MIN_TAKE_PROFIT = float(os.getenv('MIN_TAKE_PROFIT', 0.5))
    MAX_TAKE_PROFIT = float(os.getenv('MAX_TAKE_PROFIT', 50))
    
    # Trailing stop лимиты
    MIN_TRAILING_DISTANCE = float(os.getenv('MIN_TRAILING_DISTANCE', 0.1))
    MAX_TRAILING_DISTANCE = float(os.getenv('MAX_TRAILING_DISTANCE', 10))
    MIN_TRAILING_ACTIVATION = float(os.getenv('MIN_TRAILING_ACTIVATION', 0.1))
    MAX_TRAILING_ACTIVATION = float(os.getenv('MAX_TRAILING_ACTIVATION', 10))
    
    # Временные фильтры лимиты
    MIN_HIDE_YOUNGER = int(os.getenv('MIN_HIDE_YOUNGER', 0))
    MAX_HIDE_YOUNGER = int(os.getenv('MAX_HIDE_YOUNGER', 48))
    MIN_HIDE_OLDER = int(os.getenv('MIN_HIDE_OLDER', 1))
    MAX_HIDE_OLDER = int(os.getenv('MAX_HIDE_OLDER', 168))
    
    # Score лимиты
    MIN_SCORE = float(os.getenv('MIN_SCORE', 0))
    MAX_SCORE = float(os.getenv('MAX_SCORE', 100))
    
    # ============================================
    # АНАЛИЗ И ОПТИМИЗАЦИЯ
    # ============================================
    
    # Анализ эффективности
    EFFICIENCY_ANALYSIS_MAX_COMBINATIONS = int(os.getenv('EFFICIENCY_ANALYSIS_MAX_COMBINATIONS', 1000))
    EFFICIENCY_ANALYSIS_TIMEOUT = int(os.getenv('EFFICIENCY_ANALYSIS_TIMEOUT', 300))
    
    # Trailing анализ
    TRAILING_ANALYSIS_DEFAULT_ACTIVATION_MIN = float(os.getenv('TRAILING_ANALYSIS_DEFAULT_ACTIVATION_MIN', 0.5))
    TRAILING_ANALYSIS_DEFAULT_ACTIVATION_MAX = float(os.getenv('TRAILING_ANALYSIS_DEFAULT_ACTIVATION_MAX', 3.0))
    TRAILING_ANALYSIS_DEFAULT_DISTANCE_MIN = float(os.getenv('TRAILING_ANALYSIS_DEFAULT_DISTANCE_MIN', 0.5))
    TRAILING_ANALYSIS_DEFAULT_DISTANCE_MAX = float(os.getenv('TRAILING_ANALYSIS_DEFAULT_DISTANCE_MAX', 3.0))
    TRAILING_ANALYSIS_DEFAULT_STOP_LOSS_MIN = float(os.getenv('TRAILING_ANALYSIS_DEFAULT_STOP_LOSS_MIN', 1.0))
    TRAILING_ANALYSIS_DEFAULT_STOP_LOSS_MAX = float(os.getenv('TRAILING_ANALYSIS_DEFAULT_STOP_LOSS_MAX', 5.0))
    
    # Scoring анализ
    SCORING_ANALYSIS_DEFAULT_DAYS_BACK = int(os.getenv('SCORING_ANALYSIS_DEFAULT_DAYS_BACK', 30))
    SCORING_ANALYSIS_BATCH_SIZE = int(os.getenv('SCORING_ANALYSIS_BATCH_SIZE', 50))

    # 3-фазная торговая система
    PHASE1_DURATION_HOURS = int(os.getenv('PHASE1_DURATION_HOURS', 24))  # Фаза 1: Активная торговля
    PHASE2_DURATION_HOURS = int(os.getenv('PHASE2_DURATION_HOURS', 8))   # Фаза 2: Breakeven Window
    SMART_LOSS_RATE_PER_HOUR = float(os.getenv('SMART_LOSS_RATE_PER_HOUR', 0.5))  # Фаза 3: 0.5% в час

    # Ликвидация
    LIQUIDATION_THRESHOLD = float(os.getenv('LIQUIDATION_THRESHOLD', 0.9))  # Ликвидация при потере 90% маржи

    # ============================================
    # WAVE-BASED SCORING (новая система)
    # ============================================

    # Feature flag для включения/выключения новой системы
    USE_WAVE_BASED_SCORING = os.getenv('USE_WAVE_BASED_SCORING', 'True').lower() == 'true'

    # Управление капиталом
    INITIAL_CAPITAL = float(os.getenv('INITIAL_CAPITAL', 1000.0))  # Начальный капитал для симуляции

    # Фильтрация по волнам
    WAVE_INTERVAL_MINUTES = int(os.getenv('WAVE_INTERVAL_MINUTES', 15))  # Интервал волны (15 минут)

    # ============================================
    # МОНИТОРИНГ И ЛОГИРОВАНИЕ
    # ============================================
    
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')
    LOG_FILE_PATH = os.getenv('LOG_FILE_PATH', '/var/log/trading_assistant/app.log')
    LOG_MAX_BYTES = int(os.getenv('LOG_MAX_BYTES', 10485760))
    LOG_BACKUP_COUNT = int(os.getenv('LOG_BACKUP_COUNT', 5))
    
    # ============================================
    # БЕЗОПАСНОСТЬ
    # ============================================
    
    MAX_LOGIN_ATTEMPTS = int(os.getenv('MAX_LOGIN_ATTEMPTS', 5))
    LOGIN_ATTEMPT_WINDOW = int(os.getenv('LOGIN_ATTEMPT_WINDOW', 300))
    SESSION_LIFETIME = int(os.getenv('SESSION_LIFETIME', 3600))
    PERMANENT_SESSION_LIFETIME = int(os.getenv('PERMANENT_SESSION_LIFETIME', 86400))
    
    @classmethod
    def get_database_url(cls):
        """Формирует строку подключения к базе данных"""
        if os.getenv('DATABASE_URL'):
            return os.getenv('DATABASE_URL')
        
        params = [
            f"host={cls.DB_HOST}",
            f"port={cls.DB_PORT}",
            f"dbname={cls.DB_NAME}",
            f"user={cls.DB_USER}"
        ]
        
        if cls.DB_PASSWORD:
            params.append(f"password={cls.DB_PASSWORD}")
        
        # Добавляем параметры для стабильности соединения
        params.extend([
            "sslmode=disable",
            f"connect_timeout={cls.DB_CONNECT_TIMEOUT}",
            f"keepalives={cls.DB_KEEPALIVES}",
            f"keepalives_idle={cls.DB_KEEPALIVES_IDLE}",
            f"keepalives_interval={cls.DB_KEEPALIVES_INTERVAL}",
            f"keepalives_count={cls.DB_KEEPALIVES_COUNT}",
            f"tcp_user_timeout={cls.DB_TCP_USER_TIMEOUT}"
        ])
        
        return " ".join(params)
    
    @classmethod
    def get_default_user_filters(cls):
        """Возвращает дефолтные фильтры для новых пользователей"""
        return {
            'hide_younger_than_hours': cls.DEFAULT_HIDE_YOUNGER_HOURS,
            'hide_older_than_hours': cls.DEFAULT_HIDE_OLDER_HOURS,
            'stop_loss_percent': cls.DEFAULT_STOP_LOSS_PERCENT,
            'take_profit_percent': cls.DEFAULT_TAKE_PROFIT_PERCENT,
            'position_size_usd': cls.DEFAULT_POSITION_SIZE,
            'leverage': cls.DEFAULT_LEVERAGE,
            'use_trailing_stop': cls.DEFAULT_USE_TRAILING_STOP,
            'trailing_distance_pct': cls.DEFAULT_TRAILING_DISTANCE_PCT,
            'trailing_activation_pct': cls.DEFAULT_TRAILING_ACTIVATION_PCT,
            'score_week_min': cls.DEFAULT_SCORE_WEEK_MIN,
            'score_month_min': cls.DEFAULT_SCORE_MONTH_MIN,
            'allowed_hours': [int(h) for h in cls.DEFAULT_ALLOWED_HOURS.split(',')],
            'max_trades_per_15min': cls.DEFAULT_MAX_TRADES_PER_15MIN
        }
    
    @classmethod
    def validate_position_size(cls, value):
        """Валидация размера позиции"""
        return max(cls.MIN_POSITION_SIZE, min(cls.MAX_POSITION_SIZE, float(value)))
    
    @classmethod
    def validate_leverage(cls, value):
        """Валидация плеча"""
        return max(cls.MIN_LEVERAGE, min(cls.MAX_LEVERAGE, int(value)))
    
    @classmethod
    def validate_stop_loss(cls, value):
        """Валидация stop loss"""
        return max(cls.MIN_STOP_LOSS, min(cls.MAX_STOP_LOSS, float(value)))
    
    @classmethod
    def validate_take_profit(cls, value):
        """Валидация take profit"""
        return max(cls.MIN_TAKE_PROFIT, min(cls.MAX_TAKE_PROFIT, float(value)))
    
    @classmethod
    def validate_score(cls, value):
        """Валидация score"""
        return max(cls.MIN_SCORE, min(cls.MAX_SCORE, float(value)))
    
    @classmethod
    def validate_hide_younger(cls, value):
        """Валидация hide_younger_than_hours"""
        return max(cls.MIN_HIDE_YOUNGER, min(cls.MAX_HIDE_YOUNGER, int(value)))
    
    @classmethod
    def validate_hide_older(cls, value):
        """Валидация hide_older_than_hours"""
        return max(cls.MIN_HIDE_OLDER, min(cls.MAX_HIDE_OLDER, int(value)))