"""
Основное приложение Flask - Помощник Трейдера
"""
import os
import json
from datetime import datetime, timedelta, timezone
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, stream_with_context
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
import logging
# Импорт модулей приложения
from database import (Database, initialize_signals_with_params, process_signal_complete,
                      get_raw_signals, get_signal_details, get_raw_signals_stats)
from models import User, TradingData, TradingStats
from config import Config

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создание Flask приложения
app = Flask(__name__)

# Настройка SECRET_KEY из Config
app.secret_key = Config.SECRET_KEY
if Config.SECRET_KEY == 'dev-secret-key-change-in-production':
    print("WARNING: SECRET_KEY не задан, используется значение по умолчанию!")

# Настройки сессий для сервера

app.config.update(
    SESSION_COOKIE_SECURE=False,  # Для разработки - False, для продакшена - True
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax',
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),  # 30 дней для запомненных сессий
    SESSION_TYPE='filesystem' if os.getenv('SESSION_TYPE') == 'filesystem' else None,
    REMEMBER_COOKIE_DURATION=timedelta(days=30),  # Время жизни cookie "запомнить меня"
    REMEMBER_COOKIE_SECURE=False,  # Для разработки
    REMEMBER_COOKIE_HTTPONLY=True
)

# Настройка Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему для доступа к этой странице.'
login_manager.login_message_category = 'info'
login_manager.session_protection = 'strong'  # Защита от фиксации сессии

# Настройка логирования для отладки аутентификации
if Config.DEBUG_AUTH:
    import logging
    logging.basicConfig(level=logging.DEBUG)
    login_manager.logger = logging.getLogger('flask_login')
    login_manager.logger.setLevel(logging.DEBUG)

# Инициализация базы данных
# Инициализация базы данных с использованием Config
if Config.DB_HOST and Config.DB_NAME and Config.DB_USER:
    # Используем параметры из Config
    db = Database(
        host=Config.DB_HOST,
        port=Config.DB_PORT,
        database=Config.DB_NAME,
        user=Config.DB_USER,
        password=Config.DB_PASSWORD,  # Может быть None, тогда используется .pgpass
        use_pool=True
    )
    logger.info(f"База данных инициализирована: {Config.DB_HOST}:{Config.DB_PORT}/{Config.DB_NAME}")
    logger.info(f"Пул соединений: min={Config.DB_POOL_MIN_SIZE}, max={Config.DB_POOL_MAX_SIZE}")
elif os.getenv('DATABASE_URL'):
    # Fallback на DATABASE_URL если задан
    db = Database(database_url=os.getenv('DATABASE_URL'))
    logger.info("База данных инициализирована с DATABASE_URL")
else:
    logger.error("Не установлены параметры подключения к базе данных в Config или DATABASE_URL")
    exit(1)

# Инициализация схемы при первом запуске
try:
    db.initialize_schema()
    logger.info("Схема базы данных инициализирована")
except Exception as e:
    logger.error(f"Ошибка при инициализации схемы: {e}")

# Глобальный счетчик критических ошибок БД
database_critical_errors = 0
last_db_recovery_attempt = 0

# Глобальные переменные для хранения результатов анализа
# Ключ - user_id, значение - словарь с результатами различных анализов
analysis_results_cache = {
    'efficiency': {},  # Результаты анализа эффективности
    'tp_sl': {},       # Результаты анализа TP/SL
    'trailing': {}     # Результаты анализа Trailing Stop
}

@login_manager.user_loader
def load_user(user_id):
    """Загрузка пользователя для Flask-Login"""
    try:
        return User.get_by_id(db, int(user_id))
    except Exception as e:
        # При критической ошибке БД пытаемся восстановить соединение
        if 'consuming input failed' in str(e).lower() or 'eof detected' in str(e).lower():
            logger.error(f"Critical DB error in load_user: {e}")
            handle_database_critical_error()
        return None

def handle_database_critical_error():
    """Обработка критических ошибок БД с автоматическим восстановлением"""
    global database_critical_errors, last_db_recovery_attempt
    import time
    
    current_time = time.time()
    database_critical_errors += 1
    
    # Если прошло меньше 30 секунд с последней попытки, не пытаемся снова
    if current_time - last_db_recovery_attempt < 30:
        logger.warning("Skipping recovery attempt (too soon)")
        return False
    
    logger.critical(f"Database critical error #{database_critical_errors}, attempting recovery")
    last_db_recovery_attempt = current_time
    
    try:
        # Пытаемся восстановить пул
        if database_critical_errors >= 3:
            # После 3 ошибок используем emergency recovery
            logger.critical("Using emergency recovery after 3 failures")
            success = db.emergency_recovery()
            if success:
                database_critical_errors = 0
                logger.info("Emergency recovery successful")
                return True
        else:
            # Обычная переинициализация
            db.reinitialize_pool()
            logger.info("Pool reinitialized successfully")
            return True
    except Exception as e:
        logger.critical(f"Failed to recover database: {e}")
        return False

# Проверка доступа для неподтвержденных пользователей
@app.before_request
def check_user_approval():
    """Проверка подтверждения пользователя перед каждым запросом"""
    # Разрешаем доступ к публичным страницам
    public_endpoints = ['login', 'register', 'unauthorized', 'static']
    
    if request.endpoint in public_endpoints or request.path.startswith('/static/'):
        return
    
    # Обновляем активность сессии для авторизованных пользователей
    if current_user.is_authenticated:
        session.permanent = True  # Обновляем время жизни сессии при активности
        session.modified = True  # Помечаем сессию как измененную
    
    # Проверяем аутентификацию и подтверждение
    if current_user.is_authenticated:
        if not current_user.is_approved and not current_user.is_admin:
            return redirect(url_for('unauthorized'))

# Главная страница - редирект на дашборд
@app.route('/')
def index():
    """Главная страница"""
    if current_user.is_authenticated and current_user.is_approved:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

# Страница входа
@app.route('/login', methods=['GET', 'POST'])
def login():
    """Страница входа в систему"""
    if current_user.is_authenticated and current_user.is_approved:
        return redirect(url_for('dashboard'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        if not username or not password:
            flash('Введите имя пользователя и пароль', 'error')
            return render_template('login.html')
        
        # Аутентификация пользователя
        user = User.authenticate(db, username, password)
        
        if user:
            if user.is_approved or user.is_admin:
                # Проверяем, нужно ли запомнить пользователя
                remember = bool(request.form.get('remember_me'))
                login_user(user, remember=remember)
                
                # Если пользователь выбрал "запомнить меня", делаем сессию постоянной
                if remember:
                    session.permanent = True
                
                logger.info(f"Пользователь {username} вошел в систему (remember={remember})")
                
                # Перенаправление на запрошенную страницу или дашборд
                next_page = request.args.get('next')
                if not next_page or not next_page.startswith('/'):
                    next_page = url_for('dashboard')
                return redirect(next_page)
            else:
                flash('Ваша учетная запись ожидает подтверждения администратором', 'warning')
                return redirect(url_for('unauthorized'))
        else:
            flash('Неверное имя пользователя или пароль', 'error')
    
    return render_template('login.html')

def is_first_user(db):
    """Проверка, является ли регистрируемый пользователь первым в системе"""
    try:
        result = db.execute_query(
            "SELECT COUNT(*) as count FROM users",
            fetch=True
        )
        return result[0]['count'] == 0
    except:
        return True  # Если таблица не существует, считаем что это первый пользователь

# Страница регистрации
@app.route('/register', methods=['GET', 'POST'])
def register():
    """Страница регистрации"""
    if current_user.is_authenticated and current_user.is_approved:
        return redirect(url_for('dashboard'))

    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        # Валидация данных
        if not username or not password:
            flash('Заполните все поля', 'error')
            return render_template('register.html')

        if len(username) < 3:
            flash('Имя пользователя должно содержать минимум 3 символа', 'error')
            return render_template('register.html')

        if len(password) < 6:
            flash('Пароль должен содержать минимум 6 символов', 'error')
            return render_template('register.html')

        if password != confirm_password:
            flash('Пароли не совпадают', 'error')
            return render_template('register.html')

        # Проверка первого пользователя
        is_first = is_first_user(db)

        # Создание пользователя
        user = User.create(
            db,
            username,
            password,
            is_admin=is_first,  # Первый пользователь - администратор
            is_approved=is_first  # Первый пользователь сразу подтвержден
        )

        if user:
            if is_first:
                flash(f'Добро пожаловать, администратор {username}! Вы можете войти в систему.', 'success')
                logger.info(f"Создан первый пользователь-администратор: {username}")
            else:
                flash('Регистрация успешна! Ожидайте подтверждения от администратора.', 'info')
                logger.info(f"Зарегистрирован новый пользователь: {username}")

            return redirect(url_for('login'))
        else:
            flash('Пользователь с таким именем уже существует', 'error')

    return render_template('register.html')

# Выход из системы
@app.route('/api/admin/db_health')
@login_required
def check_database_health():
    """Endpoint для проверки и восстановления состояния БД (только для админов)"""
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    try:
        # Проверяем состояние пула
        pool_healthy = db.check_pool_health()
        
        # Пробуем выполнить тестовый запрос
        test_result = db.execute_query("SELECT 1 as test", fetch=True)
        
        return jsonify({
            'status': 'success',
            'pool_healthy': pool_healthy,
            'test_query': 'success' if test_result else 'failed',
            'critical_errors': database_critical_errors,
            'message': 'Database is healthy'
        })
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e),
            'critical_errors': database_critical_errors
        }), 500

@app.route('/api/admin/db_recover', methods=['POST'])
@login_required
def recover_database():
    """Endpoint для принудительного восстановления БД (только для админов)"""
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    try:
        # Принудительное восстановление
        recovery_type = request.json.get('type', 'normal')
        
        if recovery_type == 'emergency':
            logger.warning("Admin requested emergency recovery")
            success = db.emergency_recovery()
        else:
            logger.warning("Admin requested pool reinitialization")
            db.reinitialize_pool()
            success = True
        
        if success:
            global database_critical_errors
            database_critical_errors = 0
            return jsonify({
                'status': 'success',
                'message': 'Database recovered successfully'
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Recovery failed'
            }), 500
            
    except Exception as e:
        logger.error(f"Manual recovery failed: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/logout')
@login_required
def logout():
    """Выход из системы"""
    username = current_user.username
    logout_user()
    flash(f'До свидания, {username}!', 'info')
    logger.info(f"Пользователь {username} вышел из системы")
    return redirect(url_for('login'))

# Страница ожидания подтверждения
@app.route('/unauthorized')
def unauthorized():
    """Страница для неподтвержденных пользователей"""
    return render_template('unauthorized.html')

# Административная панель
@app.route('/admin')
@login_required
def admin():
    """Административная панель"""
    if not current_user.is_admin:
        flash('Доступ запрещен', 'error')
        return redirect(url_for('dashboard'))
    
    from database import get_unapproved_users
    unapproved_users = get_unapproved_users(db)
    
    return render_template('admin.html', unapproved_users=unapproved_users)

# Подтверждение пользователя администратором
@app.route('/admin/approve/<int:user_id>', methods=['POST'])
@login_required
def approve_user_route(user_id):
    """Подтверждение пользователя администратором"""
    if not current_user.is_admin:
        flash('Доступ запрещен', 'error')
        return redirect(url_for('dashboard'))
    
    try:
        from database import approve_user, get_user_by_id
        user_data = get_user_by_id(db, user_id)
        
        if user_data:
            approve_user(db, user_id)
            flash(f'Пользователь {user_data["username"]} подтвержден', 'success')
            logger.info(f"Администратор {current_user.username} подтвердил пользователя {user_data['username']}")
        else:
            flash('Пользователь не найден', 'error')
    except Exception as e:
        flash('Ошибка при подтверждении пользователя', 'error')
        logger.error(f"Ошибка при подтверждении пользователя {user_id}: {e}")
    
    return redirect(url_for('admin'))

# Главный дашборд BigGuy
@app.route('/dashboard')
@login_required
def dashboard():
    """Главный дашборд BigGuy для анализа потоков капитала"""
    # Получение параметров фильтрации из запроса
    time_filter = request.args.get('time_filter', '24h')
    min_value_usd = request.args.get('min_value_usd', type=float)
    operation_type = request.args.get('operation_type', 'both')
    
    # Валидация временного фильтра
    valid_time_filters = ['1h', '4h', '12h', '24h', '7d']
    if time_filter not in valid_time_filters:
        time_filter = '24h'
    
    # Валидация типа операций
    valid_operation_types = ['buys', 'sells', 'both']
    if operation_type not in valid_operation_types:
        operation_type = 'both'
    
    # Валидация минимальной суммы (учитываем constraint >= 10000)
    if min_value_usd is not None and min_value_usd < Config.DEFAULT_MIN_VALUE_USD:
        min_value_usd = Config.DEFAULT_MIN_VALUE_USD
    
    try:
        # Получение данных для дашборда
        trading_data = TradingData.get_dashboard_data(db, time_filter, min_value_usd, operation_type)
        trading_stats = TradingStats.get_stats(db, time_filter, min_value_usd, operation_type)
        
        # Преобразуем объекты в словари для правильной JSON сериализации
        trading_data_dicts = [item.to_dict() for item in trading_data] if trading_data else []
        trading_stats_dict = trading_stats.to_dict() if trading_stats else None
        
        return render_template(
            'dashboard.html',
            trading_data=trading_data_dicts,
            trading_stats=trading_stats_dict,
            time_filter=time_filter,
            min_value_usd=min_value_usd or Config.DEFAULT_MIN_VALUE_USD,
            operation_type=operation_type,
            time_filter_options=[
                ('1h', 'Последний час'),
                ('4h', '4 часа'),
                ('12h', '12 часов'),
                ('24h', '24 часа'),
                ('7d', '7 дней')
            ],
            operation_type_options=[
                ('both', 'Покупки и продажи'),
                ('buys', 'Только покупки'),
                ('sells', 'Только продажи')
            ]
        )
    except Exception as e:
        logger.error(f"Ошибка при загрузке дашборда: {e}")
        flash('Ошибка при загрузке данных дашборда', 'error')
        return render_template('dashboard.html', trading_data=[], trading_stats=None)

# API для получения данных дашборда (AJAX)
@app.route('/api/dashboard-data')
@login_required
def api_dashboard_data():
    """API для получения данных дашборда через AJAX"""
    time_filter = request.args.get('time_filter', '24h')
    min_value_usd = request.args.get('min_value_usd', type=float)
    operation_type = request.args.get('operation_type', 'both')
    
    # Валидация параметров
    valid_time_filters = ['1h', '4h', '12h', '24h', '7d']
    if time_filter not in valid_time_filters:
        time_filter = '24h'
    
    valid_operation_types = ['buys', 'sells', 'both']
    if operation_type not in valid_operation_types:
        operation_type = 'both'
    
    if min_value_usd is not None and min_value_usd < Config.DEFAULT_MIN_VALUE_USD:
        min_value_usd = Config.DEFAULT_MIN_VALUE_USD
    
    try:
        # Получение данных
        trading_data = TradingData.get_dashboard_data(db, time_filter, min_value_usd, operation_type)
        trading_stats = TradingStats.get_stats(db, time_filter, min_value_usd, operation_type)
        
        # Преобразование в JSON
        data = {
            'trading_data': [item.to_dict() for item in trading_data],
            'trading_stats': trading_stats.to_dict() if trading_stats else None,
            'timestamp': datetime.now().isoformat()
        }
        
        return jsonify(data)
    except Exception as e:
        logger.error(f"Ошибка API дашборда: {e}")
        return jsonify({'error': 'Ошибка при загрузке данных'}), 500


@app.route('/signal_performance')
@login_required
def signal_performance():
    """Страница отслеживания эффективности торговых сигналов"""
    print(f"[AUTH_DEBUG] signal_performance called by user: {current_user.id if current_user.is_authenticated else 'Not authenticated'}")
    print(f"[AUTH_DEBUG] current_user.is_authenticated: {current_user.is_authenticated}")

    if not current_user.is_authenticated:
        print("[AUTH_DEBUG] User not authenticated, redirecting to login")
        return redirect(url_for('login'))

    try:
        print("[AUTH_DEBUG] Starting signal_performance processing...")
        # Получаем настройки пользователя
        filters_query = """
            SELECT * FROM web.user_signal_filters 
            WHERE user_id = %s
        """
        user_filters = db.execute_query(filters_query, (current_user.id,), fetch=True)

        if user_filters:
            filters = user_filters[0]
        else:
            # Если записи нет, создаем её со значениями по умолчанию
            # Используем дефолтные значения из Config
            default_filters = Config.get_default_user_filters()
            
            # Создаем запись в БД для пользователя
            insert_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, hide_younger_than_hours, hide_older_than_hours,
                    stop_loss_percent, take_profit_percent, position_size_usd,
                    leverage, use_trailing_stop, trailing_distance_pct,
                    trailing_activation_pct, score_week_min, score_month_min,
                    allowed_hours, max_trades_per_15min
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            db.execute_query(insert_query, (
                current_user.id,
                default_filters['hide_younger_than_hours'],
                default_filters['hide_older_than_hours'],
                default_filters['stop_loss_percent'],
                default_filters['take_profit_percent'],
                default_filters['position_size_usd'],
                default_filters['leverage'],
                default_filters['use_trailing_stop'],
                default_filters['trailing_distance_pct'],
                default_filters['trailing_activation_pct'],
                default_filters['score_week_min'],
                default_filters['score_month_min'],
                default_filters['allowed_hours'],
                default_filters.get('max_trades_per_15min', 3)
            ))
            
            filters = default_filters

        # Используем параметры по умолчанию (без пользовательских фильтров)
        hide_younger = filters['hide_younger_than_hours']
        hide_older = filters['hide_older_than_hours']
        display_leverage = filters['leverage']
        display_position_size = float(filters['position_size_usd'])

        # ========== ИСПОЛЬЗУЕМ АВТОМАТИЧЕСКИЙ ПОДБОР ОПТИМАЛЬНЫХ ПАРАМЕТРОВ ИЗ БЭКТЕСТОВ ==========
        from database import get_best_scoring_signals_with_backtest_params
        from datetime import datetime, date, timedelta

        print(f"[SIGNAL_PERFORMANCE] Получаем сигналы с оптимальными параметрами из бэктестов")
        print(f"[SIGNAL_PERFORMANCE] Период: последние 48 часов")
        print(f"[SIGNAL_PERFORMANCE] Все параметры (SL, TS, max_trades) берутся из оптимальных backtest для каждой биржи")

        raw_signals, params_by_exchange = get_best_scoring_signals_with_backtest_params(db)

        print(f"[SIGNAL_PERFORMANCE] Получено {len(raw_signals) if raw_signals else 0} сигналов с оптимальными параметрами")
        print(f"[SIGNAL_PERFORMANCE] Оптимальные параметры для каждой биржи:")
        for exchange_id, params in params_by_exchange.items():
            print(f"  {params['exchange_name']} (ID={exchange_id}):")
            print(f"    SL: {params['stop_loss_filter']}%, TS Activation: {params['trailing_activation_filter']}%, TS Distance: {params['trailing_distance_filter']}%")
            print(f"    Max trades/15min: {params['max_trades_filter']}, Score week: {params['score_week_filter']}, Score month: {params['score_month_filter']}")

        # Инициализируем пустые данные
        signals_data = []
        processed_signals = []
        efficiency_metrics = {
            'total_signals': 0,
            'open_positions': 0,
            'closed_tp': 0,
            'closed_sl': 0,
            'closed_trailing': 0,
            'trailing_wins': 0,
            'trailing_losses': 0,
            'trailing_avg_profit': 0,
            'tp_avg_profit': 0,
            'trailing_efficiency': 0,
            'trailing_max_movement': 0,
            'trailing_captured': 0,
            'trailing_missed': 0,
            'trailing_capture_rate': 0,
            'tp_realized_profit': 0,
            'sl_realized_loss': 0,
            'net_realized_pnl': 0,
            'unrealized_pnl': 0,
            'total_pnl': 0,
            'total_max_potential': 0,
            'tp_max_potential': 0,
            'missed_profit': 0,
            'tp_efficiency': 0,
            'overall_efficiency': 0,
            'win_rate': 0,
            'avg_tp_percent': 0,
            'avg_sl_percent': 0
        }

        if raw_signals:
            # Обрабатываем каждый сигнал
            print("[SIGNAL_PERFORMANCE] Начинаем обработку сигналов...")

            # Получаем текущие цены для расчета unrealized P&L
            prices_query = """
                SELECT DISTINCT ON (tp.pair_symbol)
                    tp.pair_symbol,
                    md.mark_price
                FROM public.trading_pairs tp
                JOIN public.market_data md ON md.trading_pair_id = tp.id
                WHERE tp.contract_type_id = 1
                    AND tp.exchange_id IN (1, 2)
                    AND md.capture_time >= NOW() - INTERVAL '5 minutes'
                ORDER BY tp.pair_symbol, md.capture_time DESC
            """
            price_data = db.execute_query(prices_query, fetch=True)
            current_prices = {p['pair_symbol']: float(p['mark_price']) for p in price_data} if price_data else {}

            # Обрабатываем сигналы
            from database import process_signal_complete, make_aware
            import uuid

            # Генерируем ID сессии для batch обработки
            session_id = f"signal_perf_{current_user.id}_{uuid.uuid4().hex[:8]}"

            # ВАЖНО: Очищаем старые данные web_signals перед новой загрузкой
            # Используем DELETE вместо TRUNCATE для лучшей совместимости
            db.execute_query("DELETE FROM web.web_signals")
            print("[SIGNAL_PERFORMANCE] Таблица web_signals очищена")

            processed_count = 0
            error_count = 0
            skip_count = 0

            for signal in raw_signals:
                try:
                    # Проверяем, не обработан ли уже этот сигнал
                    check_query = "SELECT 1 FROM web.web_signals WHERE signal_id = %s"
                    existing = db.execute_query(check_query, (signal['signal_id'],), fetch=True)

                    if existing:
                        skip_count += 1
                        continue

                    # Подготавливаем данные сигнала
                    signal_data = {
                        'signal_id': signal['signal_id'],
                        'pair_symbol': signal['pair_symbol'],
                        'trading_pair_id': signal['trading_pair_id'],
                        'signal_action': signal['signal_action'],
                        'signal_timestamp': make_aware(signal['timestamp']),
                        'exchange_name': signal.get('exchange_name', 'Unknown'),
                        'score_week': signal.get('score_week', 0),
                        'score_month': signal.get('score_month', 0)
                    }

                    # Получаем параметры для биржи этого сигнала
                    exchange_id = signal.get('exchange_id')
                    exchange_params = params_by_exchange.get(exchange_id, {})

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
                        trailing_activation_pct=exchange_params.get('trailing_activation_filter', 1.0)
                    )

                    if result['success']:
                        processed_count += 1

                        # Добавляем обработанный сигнал в список
                        processed_signal = {
                            'signal_id': signal['signal_id'],
                            'pair_symbol': signal['pair_symbol'],
                            'signal_action': signal['signal_action'],
                            'signal_timestamp': signal['timestamp'],
                            'exchange_name': signal.get('exchange_name'),
                            'total_score': float(signal.get('total_score', 0)),
                            'indicator_score': float(signal.get('indicator_score', 0)),
                            'pattern_score': float(signal.get('pattern_score', 0)),
                            'combination_score': float(signal.get('combination_score', 0)),
                            'score_week': float(signal.get('score_week', 0)),
                            'score_month': float(signal.get('score_month', 0)),
                            'is_closed': result.get('is_closed', False),
                            'close_reason': result.get('close_reason'),
                            'realized_pnl': result.get('realized_pnl', 0),
                            'max_profit': result.get('max_profit', 0)
                        }
                        processed_signals.append(processed_signal)
                    else:
                        error_count += 1

                except Exception as e:
                    print(f"[SIGNAL_PERFORMANCE] Ошибка обработки сигнала {signal['signal_id']}: {e}")
                    error_count += 1
                    continue

            print(f"[SIGNAL_PERFORMANCE] Обработано: {processed_count}, Пропущено: {skip_count}, Ошибок: {error_count}")

            # Теперь получаем обработанные сигналы из web_signals для отображения
            # с фильтрацией по возрасту
            display_signals_query = """
                SELECT *
                FROM web.web_signals
                WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                    AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                ORDER BY signal_timestamp DESC
            """

            display_signals = db.execute_query(display_signals_query, (hide_older, hide_younger), fetch=True)

            # Обрабатываем сигналы для отображения
            if display_signals:
                for signal in display_signals:
                    entry_price = float(signal['entry_price']) if signal['entry_price'] else 0

                    # Определяем текущую цену
                    if signal['is_closed']:
                        current_price = float(signal['closing_price']) if signal['closing_price'] else None
                    else:
                        last_known = float(signal['last_known_price']) if signal['last_known_price'] else 0
                        current_price = current_prices.get(signal['pair_symbol'], last_known)

                    # Рассчитываем процент изменения
                    if current_price and entry_price:
                        if signal['signal_action'] in ['SELL', 'SHORT']:
                            price_change_percent = ((entry_price - current_price) / entry_price) * 100
                        else:
                            price_change_percent = ((current_price - entry_price) / entry_price) * 100
                    else:
                        price_change_percent = 0

                    # Пересчитываем P&L с новыми параметрами
                    display_pnl = display_position_size * (price_change_percent / 100) * display_leverage

                    # Максимальный профит
                    max_profit = float(signal['max_potential_profit_usd']) if signal['max_potential_profit_usd'] else 0

                    # Возраст сигнала
                    from datetime import datetime, timezone
                    age_hours = (datetime.now(timezone.utc) - signal['signal_timestamp']).total_seconds() / 3600

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
                        'status': 'open' if not signal['is_closed'] else 
                                 ('tp' if signal['close_reason'] == 'take_profit' else 
                                  ('sl' if signal['close_reason'] == 'stop_loss' else 
                                   ('trailing' if signal['close_reason'] == 'trailing_stop' else 'closed')))
                    }

                    signals_data.append(signal_data)

            # Рассчитываем статистику эффективности
            efficiency_query = """
                WITH signal_stats AS (
                    SELECT 
                        COUNT(*) as total_signals,
                        COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_positions,
                        COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as closed_tp,
                        COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as closed_sl,
                        COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as closed_trailing,
                        COUNT(CASE WHEN close_reason = 'timeout' THEN 1 END) as closed_timeout,
                        COUNT(CASE 
                            WHEN close_reason = 'trailing_stop' AND realized_pnl_usd > 0 
                            THEN 1 
                        END) as trailing_wins,
                        COUNT(CASE 
                            WHEN close_reason = 'trailing_stop' AND realized_pnl_usd <= 0 
                            THEN 1 
                        END) as trailing_losses,
                        -- Средние проценты P&L по типам выхода
                        AVG(CASE WHEN close_reason = 'take_profit' 
                            THEN take_profit_percent END) as avg_tp_percent,
                        COALESCE(AVG(CASE WHEN close_reason = 'stop_loss' 
                            THEN ABS(realized_pnl_usd / NULLIF(position_size_usd, 0) * 100.0 / NULLIF(leverage, 1)) END), 0) as avg_sl_percent,
                        AVG(CASE WHEN close_reason = 'trailing_stop' AND realized_pnl_usd > 0
                            THEN trailing_stop_percent END) as avg_trailing_percent,
                        -- Суммы для расчета прибылей/убытков
                        COALESCE(SUM(CASE WHEN close_reason = 'take_profit' THEN realized_pnl_usd END), 0) as tp_realized_profit,
                        COALESCE(SUM(CASE WHEN close_reason = 'stop_loss' THEN realized_pnl_usd END), 0) as sl_realized_loss,
                        COALESCE(SUM(realized_pnl_usd), 0) as total_realized,
                        COALESCE(SUM(unrealized_pnl_usd), 0) as total_unrealized,
                        COALESCE(SUM(max_potential_profit_usd), 0) as total_max_potential,
                        COALESCE(SUM(CASE WHEN close_reason = 'take_profit' THEN max_potential_profit_usd END), 0) as tp_max_potential
                    FROM web.web_signals
                    WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                        AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                )
                SELECT * FROM signal_stats
            """

            eff_stats = db.execute_query(efficiency_query, (hide_older, hide_younger), fetch=True)

            if eff_stats:
                raw_stats = eff_stats[0]
                
                # Создаем объект stats с правильными именами полей для шаблона
                stats = {
                    'total': raw_stats['total_signals'],
                    'open': raw_stats['open_positions'],
                    'closed_tp': raw_stats['closed_tp'] or 0,
                    'closed_sl': raw_stats['closed_sl'] or 0,
                    'win_rate': 0  # Будет рассчитано ниже
                }
                
                efficiency_metrics['total_signals'] = raw_stats['total_signals']
                efficiency_metrics['open_positions'] = raw_stats['open_positions']
                efficiency_metrics['closed_tp'] = raw_stats['closed_tp'] or 0
                efficiency_metrics['closed_sl'] = raw_stats['closed_sl'] or 0
                efficiency_metrics['closed_trailing'] = raw_stats['closed_trailing'] or 0
                efficiency_metrics['closed_timeout'] = raw_stats['closed_timeout'] or 0
                efficiency_metrics['trailing_wins'] = raw_stats['trailing_wins'] or 0
                efficiency_metrics['trailing_losses'] = raw_stats['trailing_losses'] or 0
                # Пересчитываем PnL с учетом display параметров если они отличаются от сохраненных
                original_position_size = float(filters.get('position_size_usd', 100))
                original_leverage = int(filters.get('leverage', 5))
                
                # Коэффициент пересчета PnL
                if original_position_size > 0 and original_leverage > 0:
                    pnl_multiplier = (display_position_size * display_leverage) / (original_position_size * original_leverage)
                else:
                    pnl_multiplier = 1.0
                
                efficiency_metrics['net_realized_pnl'] = float(raw_stats['total_realized'] or 0) * pnl_multiplier
                efficiency_metrics['unrealized_pnl'] = float(raw_stats['total_unrealized'] or 0) * pnl_multiplier
                efficiency_metrics['total_pnl'] = efficiency_metrics['net_realized_pnl'] + efficiency_metrics['unrealized_pnl']
                efficiency_metrics['total_max_potential'] = float(raw_stats['total_max_potential'] or 0) * pnl_multiplier
                efficiency_metrics['tp_realized_profit'] = float(raw_stats['tp_realized_profit'] or 0) * pnl_multiplier
                efficiency_metrics['sl_realized_loss'] = float(raw_stats['sl_realized_loss'] or 0) * pnl_multiplier
                efficiency_metrics['tp_max_potential'] = float(raw_stats['tp_max_potential'] or 0) * pnl_multiplier
                efficiency_metrics['missed_profit'] = efficiency_metrics['tp_max_potential'] - efficiency_metrics['tp_realized_profit'] if efficiency_metrics['tp_max_potential'] > 0 else 0
                
                # Средние проценты
                efficiency_metrics['avg_tp_percent'] = float(raw_stats['avg_tp_percent'] or 0)
                efficiency_metrics['avg_sl_percent'] = float(raw_stats['avg_sl_percent'] or 0)
                efficiency_metrics['avg_trailing_percent'] = float(raw_stats['avg_trailing_percent'] or 0)

                # Win rate - правильный расчет с учетом всех прибыльных позиций
                total_closed = (efficiency_metrics['closed_tp'] + 
                               efficiency_metrics['closed_sl'] + 
                               efficiency_metrics['closed_trailing'] + 
                               efficiency_metrics['closed_timeout'])
                
                if total_closed > 0:
                    # Считаем все прибыльные закрытия
                    wins = efficiency_metrics['closed_tp'] + efficiency_metrics['trailing_wins']
                    losses = efficiency_metrics['closed_sl'] + efficiency_metrics['trailing_losses']
                    
                    # Win rate
                    efficiency_metrics['win_rate'] = (wins / total_closed) * 100
                    stats['win_rate'] = efficiency_metrics['win_rate']  # Обновляем stats
                    
                    # Для отображения в шаблоне
                    efficiency_metrics['total_wins'] = wins
                    efficiency_metrics['total_losses'] = losses
                else:
                    efficiency_metrics['win_rate'] = 0
                    stats['win_rate'] = 0
                    efficiency_metrics['total_wins'] = 0
                    efficiency_metrics['total_losses'] = 0

                # TP efficiency
                if efficiency_metrics['total_max_potential'] > 0:
                    efficiency_metrics['tp_efficiency'] = (efficiency_metrics['net_realized_pnl'] /
                                                           efficiency_metrics['total_max_potential']) * 100
                
                # Дополнительные запросы для расчета метрик trailing stop
                trailing_query = """
                    SELECT 
                        -- Средний профит для trailing stop vs TP
                        AVG(CASE WHEN close_reason = 'trailing_stop' AND realized_pnl_usd > 0 
                            THEN realized_pnl_usd END) as trailing_avg_profit,
                        AVG(CASE WHEN close_reason = 'take_profit' 
                            THEN realized_pnl_usd END) as tp_avg_profit,
                        
                        -- Максимальное движение и захват для trailing
                        AVG(CASE WHEN close_reason = 'trailing_stop' 
                            THEN max_potential_profit_usd END) as trailing_max_movement,
                        SUM(CASE WHEN close_reason = 'trailing_stop' 
                            THEN realized_pnl_usd END) as trailing_captured,
                        SUM(CASE WHEN close_reason = 'trailing_stop' 
                            THEN max_potential_profit_usd - realized_pnl_usd END) as trailing_missed,
                            
                        -- Распределение точек выхода
                        COUNT(CASE WHEN close_reason = 'trailing_stop' AND 
                            realized_pnl_usd > max_potential_profit_usd * 0.8 THEN 1 END) as exit_80_100,
                        COUNT(CASE WHEN close_reason = 'trailing_stop' AND 
                            realized_pnl_usd > max_potential_profit_usd * 0.6 AND 
                            realized_pnl_usd <= max_potential_profit_usd * 0.8 THEN 1 END) as exit_60_80,
                        COUNT(CASE WHEN close_reason = 'trailing_stop' AND 
                            realized_pnl_usd > max_potential_profit_usd * 0.4 AND 
                            realized_pnl_usd <= max_potential_profit_usd * 0.6 THEN 1 END) as exit_40_60,
                        COUNT(CASE WHEN close_reason = 'trailing_stop' AND 
                            realized_pnl_usd > max_potential_profit_usd * 0.2 AND 
                            realized_pnl_usd <= max_potential_profit_usd * 0.4 THEN 1 END) as exit_20_40,
                        COUNT(CASE WHEN close_reason = 'trailing_stop' AND 
                            realized_pnl_usd <= max_potential_profit_usd * 0.2 THEN 1 END) as exit_0_20
                    FROM web.web_signals
                    WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                        AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                """
                
                trailing_stats = db.execute_query(trailing_query, (hide_older, hide_younger), fetch=True)
                
                if trailing_stats and trailing_stats[0]:
                    t_stats = trailing_stats[0]
                    
                    # Обновляем метрики
                    efficiency_metrics['trailing_avg_profit'] = float(t_stats['trailing_avg_profit'] or 0)
                    efficiency_metrics['tp_avg_profit'] = float(t_stats['tp_avg_profit'] or 0)
                    efficiency_metrics['trailing_max_movement'] = float(t_stats['trailing_max_movement'] or 0)
                    efficiency_metrics['trailing_captured'] = float(t_stats['trailing_captured'] or 0)
                    efficiency_metrics['trailing_missed'] = float(t_stats['trailing_missed'] or 0)
                    
                    # Trailing efficiency - процент захвата от максимального движения
                    if efficiency_metrics['trailing_max_movement'] > 0 and efficiency_metrics['closed_trailing'] > 0:
                        total_trailing_max = efficiency_metrics['trailing_max_movement'] * efficiency_metrics['closed_trailing']
                        if total_trailing_max > 0:
                            efficiency_metrics['trailing_efficiency'] = (efficiency_metrics['trailing_captured'] / total_trailing_max) * 100
                    
                    # Trailing capture rate
                    if (efficiency_metrics['trailing_captured'] + efficiency_metrics['trailing_missed']) > 0:
                        efficiency_metrics['trailing_capture_rate'] = (
                            efficiency_metrics['trailing_captured'] / 
                            (efficiency_metrics['trailing_captured'] + efficiency_metrics['trailing_missed'])
                        ) * 100
                    
                    # Распределение точек выхода
                    efficiency_metrics['exit_distribution'] = {
                        '80-100%': int(t_stats['exit_80_100'] or 0),
                        '60-80%': int(t_stats['exit_60_80'] or 0),
                        '40-60%': int(t_stats['exit_40_60'] or 0),
                        '20-40%': int(t_stats['exit_20_40'] or 0),
                        '0-20%': int(t_stats['exit_0_20'] or 0)
                    }
                
                # Overall efficiency - общая эффективность стратегии
                if efficiency_metrics['total_max_potential'] > 0:
                    efficiency_metrics['overall_efficiency'] = (
                        efficiency_metrics['total_pnl'] / efficiency_metrics['total_max_potential']
                    ) * 100

        # Общая статистика без фильтров
        total_stats_query = """
            SELECT 
                COUNT(*) as total_all,
                COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_all,
                COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as tp_all,
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_all,
                COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as trailing_all
            FROM web.web_signals
        """
        total_stats = db.execute_query(total_stats_query, fetch=True)[0]

        # Простая статистика для отображения
        # Также пересчитываем сумму отдельных PnL для проверки
        calculated_total_pnl = sum(signal.get('pnl_usd', 0) for signal in signals_data)
        
        stats = {
            'total': len(signals_data),
            'open': efficiency_metrics['open_positions'],
            'closed_tp': efficiency_metrics['closed_tp'],
            'closed_sl': efficiency_metrics['closed_sl'],
            'total_pnl': calculated_total_pnl,  # Используем сумму из отображаемых значений
            'win_rate': efficiency_metrics['win_rate']
        }
        
        # Обновляем efficiency_metrics для согласованности
        efficiency_metrics['total_pnl'] = calculated_total_pnl

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
                'use_trailing_stop': True,  # Всегда используем trailing stop с оптимальными параметрами
                'trailing_distance_pct': 0.0,  # Устанавливается автоматически для каждой биржи
                'trailing_activation_pct': 0.0,  # Устанавливается автоматически для каждой биржи
                'score_week_min': 0,  # Фильтруется автоматически в SQL запросе
                'score_month_min': 0,  # Фильтруется автоматически в SQL запросе
                'allowed_hours': list(range(24)),  # Все часы разрешены (без фильтрации)
                'max_trades_per_15min': 0  # Устанавливается автоматически для каждой биржи
            },
            last_update=datetime.now()
        )

    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы сигналов: {e}")
        import traceback
        logger.error(traceback.format_exc())
        flash('Ошибка при загрузке данных сигналов', 'error')
        return redirect(url_for('dashboard'))


@app.route('/scoring_analysis_v2')
@login_required
def scoring_analysis_v2():
    """Расширенная версия анализа скоринга с фильтром по 15 минутам"""
    try:
        # Получаем диапазон дат
        date_range = db.execute_query("""
            SELECT 
                MIN(timestamp)::date as min_date,
                (CURRENT_DATE - INTERVAL '2 days')::date as max_date
            FROM fas_v2.scoring_history
        """, fetch=True)[0]

        # Получаем выбранную дату или используем максимальную
        selected_date = request.args.get('date', str(date_range['max_date']))

        # Получаем сохраненные фильтры пользователя
        from database import get_user_scoring_filters
        saved_filters = get_user_scoring_filters(db, current_user.id)
        
        # Получаем настройки режима торговли и параметры из user_signal_filters
        settings_query = """
            SELECT use_trailing_stop, trailing_distance_pct, trailing_activation_pct,
                   take_profit_percent, stop_loss_percent, position_size_usd, leverage,
                   allowed_hours, initial_capital
            FROM web.user_signal_filters
            WHERE user_id = %s
        """
        user_settings = db.execute_query(settings_query, (current_user.id,), fetch=True)
        
        # Значения по умолчанию
        mode = 'Fixed TP/SL'
        tp_percent = 4.0
        sl_percent = 3.0
        position_size = 100.0
        leverage = 5
        trailing_distance = 2.0
        trailing_activation = 1.0
        allowed_hours = list(range(24))
        initial_capital = Config.INITIAL_CAPITAL
        
        if user_settings:
            settings = user_settings[0]
            mode = 'Trailing Stop' if settings['use_trailing_stop'] else 'Fixed TP/SL'
            tp_percent = float(settings.get('take_profit_percent', 4.0))
            sl_percent = float(settings.get('stop_loss_percent', 3.0))
            position_size = float(settings.get('position_size_usd', 100.0))
            leverage = int(settings.get('leverage', 5))
            trailing_distance = float(settings.get('trailing_distance_pct', 2.0))
            trailing_activation = float(settings.get('trailing_activation_pct', 1.0))
            allowed_hours = settings.get('allowed_hours', list(range(24)))
            initial_capital = float(settings.get('initial_capital', Config.INITIAL_CAPITAL))

        # Форматируем allowed_hours для отображения
        hours_display = ""
        if allowed_hours and len(allowed_hours) < 24:
            # Группируем часы в диапазоны для компактного отображения
            sorted_hours = sorted(allowed_hours)
            ranges = []
            start = sorted_hours[0]
            end = sorted_hours[0]
            
            for hour in sorted_hours[1:]:
                if hour == end + 1:
                    end = hour
                else:
                    if start == end:
                        ranges.append(str(start))
                    else:
                        ranges.append(f"{start}-{end}")
                    start = hour
                    end = hour
            
            if start == end:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{end}")
            
            hours_display = f"Часы (UTC): {', '.join(ranges)}"
        else:
            hours_display = "Часы (UTC): Все"

        return render_template(
            'scoring_analysis_v2.html',
            date_range=date_range,
            selected_date=selected_date,
            saved_filters=saved_filters,
            params={
                'mode': mode,
                'tp_percent': tp_percent,
                'sl_percent': sl_percent,
                'position_size_usd': position_size,
                'leverage': leverage,
                'trailing_distance': trailing_distance,
                'trailing_activation': trailing_activation,
                'initial_capital': initial_capital
            },
            hours_display=hours_display
        )

    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы скоринга V2: {e}")
        import traceback
        logger.error(traceback.format_exc())
        flash('Ошибка при загрузке данных скоринга', 'error')
        return redirect(url_for('dashboard'))


@app.route('/api/scoring/apply_filters_v2', methods=['POST'])
@login_required
def api_scoring_apply_filters_v2():
    """API для расширенной версии скоринга с фильтром по 15 минутам"""
    try:
        data = request.get_json()

        from database import get_scoring_signals_v2, process_scoring_signals_batch, process_scoring_signals_batch_v2, get_scoring_analysis_results
        from config import Config
        import uuid

        # Генерируем ID сессии
        session_id = f"scoring_v2_{current_user.id}_{uuid.uuid4().hex[:8]}"

        # Получаем параметры
        selected_date = data.get('date')
        score_week_min = data.get('score_week_min')
        score_month_min = data.get('score_month_min')
        max_trades_per_15min = data.get('max_trades_per_15min', 3)
        
        # Получаем настройки пользователя включая allowed_hours и initial_capital
        settings_query = """
            SELECT take_profit_percent, stop_loss_percent, position_size_usd, leverage,
                   use_trailing_stop, trailing_distance_pct, trailing_activation_pct,
                   allowed_hours, initial_capital
            FROM web.user_signal_filters
            WHERE user_id = %s
        """
        user_settings = db.execute_query(settings_query, (current_user.id,), fetch=True)

        # Значения по умолчанию
        tp_percent = 4.0
        sl_percent = 3.0
        position_size = 100.0
        leverage = 5
        use_trailing_stop = False
        trailing_distance_pct = 2.0
        trailing_activation_pct = 1.0
        allowed_hours = list(range(24))  # По умолчанию все часы
        initial_capital = Config.INITIAL_CAPITAL  # По умолчанию из конфига

        if user_settings:
            settings = user_settings[0]
            tp_percent = float(settings.get('take_profit_percent', 4.0))
            sl_percent = float(settings.get('stop_loss_percent', 3.0))
            position_size = float(settings.get('position_size_usd', 100.0))
            leverage = int(settings.get('leverage', 5))
            use_trailing_stop = settings.get('use_trailing_stop', False)
            trailing_distance_pct = float(settings.get('trailing_distance_pct', 2.0))
            trailing_activation_pct = float(settings.get('trailing_activation_pct', 1.0))
            # Получаем allowed_hours из настроек пользователя
            allowed_hours = settings.get('allowed_hours', list(range(24)))
            if not allowed_hours:
                allowed_hours = list(range(24))
            # Получаем initial_capital из настроек пользователя
            initial_capital = float(settings.get('initial_capital', Config.INITIAL_CAPITAL))
        
        # Если в запросе переданы параметры, используем их
        tp_percent = data.get('tp_percent', tp_percent)
        sl_percent = data.get('sl_percent', sl_percent)
        position_size = data.get('position_size', position_size)
        leverage = data.get('leverage', leverage)
        initial_capital = data.get('initial_capital', initial_capital)

        print(f"[API V2] Обработка фильтров для даты {selected_date}")
        print(f"[API V2] Фильтры: score_week >= {score_week_min}, score_month >= {score_month_min}")
        print(f"[API V2] Максимум сделок за 15 минут: {max_trades_per_15min}")
        
        if allowed_hours is None or len(allowed_hours) == 0:
            print(f"[API V2] Разрешенные часы: Не заданы")
        elif len(allowed_hours) == 24:
            print(f"[API V2] Разрешенные часы: Все 24 часа (фильтр не применяется)")
        else:
            print(f"[API V2] Разрешенные часы: {sorted(allowed_hours)[:10]}... ({len(allowed_hours)} часов)")
        
        print(f"[API V2] Режим: {'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'}")

        # Получаем сигналы с учетом фильтра по 15 минутам
        raw_signals = get_scoring_signals_v2(db, selected_date, score_week_min, score_month_min, 
                                            allowed_hours, max_trades_per_15min)

        if raw_signals:
            print(f"[API V2] Найдено {len(raw_signals)} сигналов после фильтрации")

            # Обрабатываем сигналы - используем v2 если включен feature flag
            if Config.USE_WAVE_BASED_SCORING:
                print(f"[API V2] Используется WAVE-BASED SCORING (v2)")
                print(f"[API V2] Initial Capital: ${initial_capital}")
                result = process_scoring_signals_batch_v2(
                    db, raw_signals, session_id, current_user.id,
                    tp_percent=tp_percent,
                    sl_percent=sl_percent,
                    position_size=position_size,
                    leverage=leverage,
                    use_trailing_stop=use_trailing_stop,
                    trailing_distance_pct=trailing_distance_pct,
                    trailing_activation_pct=trailing_activation_pct,
                    max_trades_per_15min=max_trades_per_15min,
                    initial_capital=initial_capital
                )
            else:
                print(f"[API V2] Используется СТАРАЯ ВЕРСИЯ (v1)")
                result = process_scoring_signals_batch(
                    db, raw_signals, session_id, current_user.id,
                    tp_percent=tp_percent,
                    sl_percent=sl_percent,
                    position_size=position_size,
                    leverage=leverage,
                    use_trailing_stop=use_trailing_stop,
                    trailing_distance_pct=trailing_distance_pct,
                    trailing_activation_pct=trailing_activation_pct
                )

            # Получаем обработанные результаты
            signals_data = get_scoring_analysis_results(db, session_id, current_user.id)

            # Форматируем для отображения
            formatted_signals = []
            for signal in signals_data:
                formatted_signals.append({
                    'timestamp': signal['signal_timestamp'].isoformat() if signal['signal_timestamp'] else None,
                    'pair_symbol': signal['pair_symbol'],
                    'exchange_name': signal.get('exchange_name', 'Unknown'),
                    'signal_action': signal['signal_action'],
                    'market_regime': signal['market_regime'],
                    'total_score': float(signal['total_score'] or 0),
                    'score_week': float(signal.get('score_week', 0)) if signal.get('score_week') else None,
                    'score_month': float(signal.get('score_month', 0)) if signal.get('score_month') else None,
                    'entry_price': float(signal['entry_price']) if signal['entry_price'] else 0,
                    'current_price': float(signal['close_price']) if signal['close_price'] else 0,
                    'is_closed': signal['is_closed'],
                    'close_reason': signal['close_reason'],
                    'hours_to_close': float(signal['hours_to_close'] or 0),
                    'pnl_usd': float(signal['pnl_usd'] or 0),
                    'pnl_percent': float(signal['pnl_percent'] or 0),
                    'max_potential_profit': float(signal['max_potential_profit_usd'] or 0)
                })

            # Статистика
            stats_data = result['stats']

            stats = {
                'total': int(stats_data.get('total') or 0),
                'buy_signals': int(stats_data.get('buy_signals') or 0),
                'sell_signals': int(stats_data.get('sell_signals') or 0),
                'tp_count': int(stats_data.get('tp_count') or 0),
                'sl_count': int(stats_data.get('sl_count') or 0),
                'trailing_count': int(stats_data.get('trailing_count') or 0),
                'timeout_count': int(stats_data.get('timeout_count') or 0),
                'open_count': int(stats_data.get('open_count') or 0),
                'total_pnl': float(stats_data.get('total_pnl') or 0)
            }

            return jsonify({
                'status': 'success',
                'data': {
                    'signals': formatted_signals,
                    'stats': stats
                }
            })
        else:
            return jsonify({
                'status': 'success',
                'data': {
                    'signals': [],
                    'stats': {
                        'total': 0,
                        'buy_signals': 0,
                        'sell_signals': 0,
                        'tp_count': 0,
                        'sl_count': 0,
                        'trailing_count': 0,
                        'timeout_count': 0,
                        'open_count': 0,
                        'total_pnl': 0
                    }
                }
            })

    except Exception as e:
        logger.error(f"Ошибка в API scoring V2: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/scoring/get_date_info_v2', methods=['POST'])
@login_required
def api_scoring_get_date_info_v2():
    """API для получения информации о сигналах с учетом фильтра 15 минут"""
    try:
        data = request.get_json()
        selected_date = data.get('date')
        score_week_min = data.get('score_week', 0)
        score_month_min = data.get('score_month', 0)
        max_trades_per_15min = data.get('max_trades_per_15min', 3)

        # Получаем общее количество сигналов
        count_query = """
            SELECT COUNT(*) as total
            FROM fas_v2.scoring_history sh
            JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
            WHERE sh.timestamp::date = %s
                AND sh.score_week >= %s
                AND sh.score_month >= %s
                AND tp.contract_type_id = 1
                AND tp.exchange_id IN (1, 2)
        """
        
        result = db.execute_query(count_query, 
                                 (selected_date, score_week_min, score_month_min), 
                                 fetch=True)
        total_count = result[0]['total'] if result else 0

        # Получаем количество после фильтрации по 15 минутам
        from database import get_scoring_signals_v2
        filtered_signals = get_scoring_signals_v2(db, selected_date, score_week_min, 
                                                 score_month_min, list(range(24)), 
                                                 max_trades_per_15min)
        filtered_count = len(filtered_signals) if filtered_signals else 0

        return jsonify({
            'status': 'success',
            'signal_count': total_count,
            'filtered_count': filtered_count
        })

    except Exception as e:
        logger.error(f"Ошибка в API get_date_info_v2: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/scoring/apply_filters', methods=['POST'])
@login_required
def api_scoring_apply_filters():
    """API для применения упрощенных фильтров скоринга с поддержкой Trailing Stop"""
    try:
        data = request.get_json()

        from database import get_scoring_signals_v2, process_scoring_signals_batch, get_scoring_analysis_results
        import uuid

        # Генерируем ID сессии
        session_id = f"scoring_{current_user.id}_{uuid.uuid4().hex[:8]}"

        # Получаем параметры
        selected_date = data.get('date')
        score_week_min = data.get('score_week_min')
        score_month_min = data.get('score_month_min')
        max_trades_per_15min = data.get('max_trades_per_15min', 3)
        
        # Получаем ВСЕ настройки пользователя из user_signal_filters
        settings_query = """
            SELECT take_profit_percent, stop_loss_percent, position_size_usd, leverage,
                   use_trailing_stop, trailing_distance_pct, trailing_activation_pct,
                   allowed_hours
            FROM web.user_signal_filters
            WHERE user_id = %s
        """
        user_settings = db.execute_query(settings_query, (current_user.id,), fetch=True)

        # Значения по умолчанию
        tp_percent = 4.0
        sl_percent = 3.0
        position_size = 100.0
        leverage = 5
        use_trailing_stop = False
        trailing_distance_pct = 2.0
        trailing_activation_pct = 1.0
        allowed_hours = list(range(24))

        if user_settings:
            settings = user_settings[0]
            # Загружаем сохраненные параметры TP/SL
            tp_percent = float(settings.get('take_profit_percent', 4.0))
            sl_percent = float(settings.get('stop_loss_percent', 3.0))
            position_size = float(settings.get('position_size_usd', 100.0))
            leverage = int(settings.get('leverage', 5))
            # Загружаем параметры trailing stop
            use_trailing_stop = settings.get('use_trailing_stop', False)
            trailing_distance_pct = float(settings.get('trailing_distance_pct', 2.0))
            trailing_activation_pct = float(settings.get('trailing_activation_pct', 1.0))
            # Загружаем разрешенные часы
            allowed_hours = settings.get('allowed_hours', list(range(24)))
            if not allowed_hours:
                allowed_hours = list(range(24))
        
        # Если в запросе переданы параметры, используем их (для ручного переопределения)
        tp_percent = data.get('tp_percent', tp_percent)
        sl_percent = data.get('sl_percent', sl_percent)
        position_size = data.get('position_size', position_size)
        leverage = data.get('leverage', leverage)

        print(f"[API] Обработка фильтров для даты {selected_date}")
        print(f"[API] Фильтры: score_week >= {score_week_min}, score_month >= {score_month_min}")
        print(f"[API] Режим: {'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'}")
        print(f"[API] Разрешенные часы: {allowed_hours if allowed_hours else 'Все'}")
        print(f"[API] Количество разрешенных часов: {len(allowed_hours) if allowed_hours else 24}")

        # Получаем сигналы по упрощенным фильтрам с учетом разрешенных часов
        raw_signals = get_scoring_signals_v2(db, selected_date, score_week_min, score_month_min, allowed_hours, max_trades_per_15min)

        if raw_signals:
            print(f"[API] Найдено {len(raw_signals)} сигналов")

            # Обрабатываем с учетом trailing stop
            result = process_scoring_signals_batch(
                db, raw_signals, session_id, current_user.id,
                tp_percent=tp_percent,
                sl_percent=sl_percent,
                position_size=position_size,
                leverage=leverage,
                use_trailing_stop=use_trailing_stop,
                trailing_distance_pct=trailing_distance_pct,
                trailing_activation_pct=trailing_activation_pct
            )

            # Получаем обработанные результаты из БД
            signals_data = get_scoring_analysis_results(db, session_id, current_user.id)

            # Форматируем для отображения
            formatted_signals = []
            for signal in signals_data:
                formatted_signals.append({
                    'timestamp': signal['signal_timestamp'].isoformat() if signal['signal_timestamp'] else None,
                    'pair_symbol': signal['pair_symbol'],
                    'exchange_name': signal.get('exchange_name', 'Unknown'),
                    'signal_action': signal['signal_action'],
                    'market_regime': signal['market_regime'],
                    'total_score': float(signal['total_score'] or 0),
                    'indicator_score': float(signal['indicator_score'] or 0),
                    'pattern_score': float(signal['pattern_score'] or 0),
                    'combination_score': float(signal['combination_score'] or 0),
                    'score_week': float(signal.get('score_week', 0)) if signal.get('score_week') else None,
                    'score_month': float(signal.get('score_month', 0)) if signal.get('score_month') else None,
                    'entry_price': float(signal['entry_price']) if signal['entry_price'] else 0,
                    'current_price': float(signal['close_price']) if signal['close_price'] else 0,
                    'is_closed': signal['is_closed'],
                    'close_reason': signal['close_reason'],
                    'hours_to_close': float(signal['hours_to_close'] or 0),
                    'pnl_usd': float(signal['pnl_usd'] or 0),
                    'pnl_percent': float(signal['pnl_percent'] or 0),
                    'max_potential_profit': float(signal['max_potential_profit_usd'] or 0)
                })

            # Статистика с учетом trailing stops
            stats_data = result['stats']

            # ИСПРАВЛЕНО: Теперь счетчики корректно разделены
            tp_count = int(stats_data.get('tp_count') or 0)
            sl_count = int(stats_data.get('sl_count') or 0)
            trailing_count = int(stats_data.get('trailing_count') or 0)

            stats = {
                'total': int(stats_data.get('total') or 0),
                'buy_signals': int(stats_data.get('buy_signals') or 0),
                'sell_signals': int(stats_data.get('sell_signals') or 0),
                'tp_count': tp_count,  # Только настоящие TP
                'sl_count': sl_count,  # Только настоящие SL
                'trailing_count': trailing_count,  # Все trailing stops
                'trailing_wins': int(stats_data.get('trailing_wins') or 0),  # Прибыльные trailing
                'trailing_losses': int(stats_data.get('trailing_losses') or 0),  # Убыточные trailing
                'timeout_count': int(stats_data.get('timeout_count') or 0),
                'total_pnl': float(stats_data.get('total_pnl') or 0),
                'realized_profit': float(stats_data.get('tp_profit') or 0),  # Прибыль от TP
                'realized_loss': float(stats_data.get('sl_loss') or 0),  # Убытки от SL
                'trailing_pnl': float(stats_data.get('trailing_pnl') or 0),  # P&L от trailing
                'max_potential': float(stats_data.get('total_max_potential') or 0),
                'avg_hours_to_close': float(stats_data.get('avg_hours_to_close') or 0),
                'binance_signals': int(stats_data.get('binance_signals') or 0),
                'bybit_signals': int(stats_data.get('bybit_signals') or 0),
                'mode': 'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'
            }

            # Расчет метрик с учетом trailing stops
            # Для win rate считаем все прибыльные позиции (TP + прибыльные trailing)
            total_wins = stats['tp_count'] + stats['trailing_wins']
            total_losses = stats['sl_count'] + stats['trailing_losses']
            total_closed = total_wins + total_losses
            win_rate = (total_wins / total_closed * 100) if total_closed > 0 else 0

            # Эффективность с учетом всех прибыльных выходов
            total_realized_profit = stats['realized_profit'] + max(0, stats['trailing_pnl'])
            tp_efficiency = 0
            if stats['max_potential'] > 0:
                tp_efficiency = (total_realized_profit / stats['max_potential']) * 100

            metrics = {
                'win_rate': win_rate,
                'tp_efficiency': tp_efficiency,
                'net_pnl': stats['total_pnl'],
                'mode': stats['mode'],
                'trailing_effectiveness': (stats['trailing_pnl'] / (abs(stats['trailing_pnl']) + 0.01)) * 100 if stats['trailing_count'] > 0 else 0
            }

            print(f"[API] Обработано: Total={stats['total']}, TP={stats['tp_count']}, "
                  f"SL={stats['sl_count']}, Trailing={stats['trailing_count']}, P&L=${stats['total_pnl']:.2f}")

            return jsonify({
                'status': 'success',
                'data': {
                    'signals': formatted_signals,
                    'stats': stats,
                    'metrics': metrics,
                    'exchange_breakdown': {
                        'Binance': stats['binance_signals'],
                        'Bybit': stats['bybit_signals']
                    }
                }
            })
        else:
            # Нет сигналов
            return jsonify({
                'status': 'success',
                'data': {
                    'signals': [],
                    'stats': {
                        'total': 0,
                        'mode': 'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'
                    },
                    'metrics': {
                        'win_rate': 0,
                        'tp_efficiency': 0,
                        'net_pnl': 0
                    }
                }
            })

    except Exception as e:
        logger.error(f"Ошибка API скоринга: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/scoring/save_filters', methods=['POST'])
@login_required
def api_scoring_save_filters():
    """API для сохранения упрощенных фильтров скоринга"""
    try:
        data = request.get_json()

        from datetime import datetime

        filter_name = data.get('name', f'Filter_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
        score_week_min = data.get('score_week_min')
        score_month_min = data.get('score_month_min')

        # Сохраняем в упрощенном формате
        save_query = """
            INSERT INTO web.user_scoring_filters (
                user_id, filter_name, buy_filters, sell_filters
            ) VALUES (%s, %s, %s, %s)
            ON CONFLICT (user_id, filter_name) DO UPDATE SET
                buy_filters = EXCLUDED.buy_filters,
                sell_filters = EXCLUDED.sell_filters,
                updated_at = NOW()
        """

        import json
        # Сохраняем параметры  в JSON для обратной совместимости
        filter_data = {
            'score_week_min': score_week_min,
            'score_month_min': score_month_min
        }

        db.execute_query(save_query, (
            current_user.id,
            filter_name,
            json.dumps(filter_data),  # Используем buy_filters для хранения
            json.dumps({})  # sell_filters оставляем пустым
        ))

        return jsonify({
            'status': 'success',
            'message': 'Фильтры сохранены'
        })

    except Exception as e:
        logger.error(f"Ошибка сохранения фильтров: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/scoring/save_trading_params', methods=['POST'])
@login_required
def api_scoring_save_trading_params():
    """API для сохранения параметров торговли (position_size, leverage, initial_capital)"""
    try:
        data = request.get_json()

        position_size = float(data.get('position_size', 100))
        leverage = int(data.get('leverage', 5))
        initial_capital = float(data.get('initial_capital', 1000))

        update_query = """
            UPDATE web.user_signal_filters SET
                position_size_usd = %s,
                leverage = %s,
                initial_capital = %s,
                updated_at = NOW()
            WHERE user_id = %s
        """
        db.execute_query(update_query, (position_size, leverage, initial_capital, current_user.id))

        return jsonify({
            'status': 'success',
            'message': 'Параметры торговли сохранены'
        })

    except Exception as e:
        logger.error(f"Ошибка сохранения параметров торговли: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/save_trading_mode', methods=['POST'])
@login_required
def api_save_trading_mode():
    """Сохранение режима управления позицией (Fixed/Trailing) без переинициализации"""
    try:
        data = request.get_json()
        use_trailing_stop = data.get('use_trailing_stop', False)
        
        if use_trailing_stop:
            # Сохраняем параметры Trailing Stop
            trailing_distance = float(data.get('trailing_distance', 2.0))
            trailing_activation = float(data.get('trailing_activation', 1.0))
            trailing_stop_loss = float(data.get('trailing_stop_loss', 3.0))
            
            update_query = """
                UPDATE web.user_signal_filters SET
                    use_trailing_stop = %s,
                    trailing_distance_pct = %s,
                    trailing_activation_pct = %s,
                    stop_loss_percent = %s,
                    updated_at = NOW()
                WHERE user_id = %s
            """
            db.execute_query(update_query, (
                True, trailing_distance, trailing_activation, 
                trailing_stop_loss, current_user.id
            ))
        else:
            # Сохраняем параметры Fixed TP/SL
            take_profit = float(data.get('take_profit', 4.0))
            stop_loss = float(data.get('stop_loss', 3.0))
            
            update_query = """
                UPDATE web.user_signal_filters SET
                    use_trailing_stop = %s,
                    take_profit_percent = %s,
                    stop_loss_percent = %s,
                    updated_at = NOW()
                WHERE user_id = %s
            """
            db.execute_query(update_query, (
                False, take_profit, stop_loss, current_user.id
            ))
        
        return jsonify({
            'status': 'success',
            'message': f"Режим {'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'} сохранен"
        })
        
    except Exception as e:
        logger.error(f"Ошибка сохранения режима управления: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

# ========== API ENDPOINTS ==========

@app.route('/api/initialize_signals', methods=['POST'])
@login_required
def api_initialize_signals():
    """Полная инициализация системы с новыми параметрами"""
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Только для администраторов'}), 403

    try:
        data = request.get_json() or {}
        hours_back = data.get('hours', 48)
        use_trailing_stop = data.get('use_trailing_stop', False)
        
        # Обрабатываем параметры в зависимости от режима
        if use_trailing_stop:
            # Режим Trailing Stop
            trailing_distance = data.get('trailing_distance', 2.0)
            trailing_activation = data.get('trailing_activation', 1.0)
            sl_percent = data.get('stop_loss', 3.0)
            tp_percent = 10.0  # Высокий TP для trailing (как виртуальный максимум)
            
            # Сохраняем параметры trailing
            update_params_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, use_trailing_stop, 
                    trailing_distance_pct, trailing_activation_pct,
                    stop_loss_percent, take_profit_percent
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    use_trailing_stop = EXCLUDED.use_trailing_stop,
                    trailing_distance_pct = EXCLUDED.trailing_distance_pct,
                    trailing_activation_pct = EXCLUDED.trailing_activation_pct,
                    stop_loss_percent = EXCLUDED.stop_loss_percent,
                    take_profit_percent = EXCLUDED.take_profit_percent,
                    updated_at = NOW()
            """
            db.execute_query(update_params_query, (
                current_user.id, True, trailing_distance, trailing_activation, sl_percent, tp_percent
            ))
            
            mode_text = f"Trailing Stop (активация: {trailing_activation}%, дистанция: {trailing_distance}%, SL: {sl_percent}%)"
        else:
            # Режим Fixed TP/SL
            tp_percent = data.get('take_profit', 4.0)
            sl_percent = data.get('stop_loss', 3.0)
            
            # Сохраняем параметры fixed
            update_params_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, use_trailing_stop,
                    stop_loss_percent, take_profit_percent
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    use_trailing_stop = EXCLUDED.use_trailing_stop,
                    stop_loss_percent = EXCLUDED.stop_loss_percent,
                    take_profit_percent = EXCLUDED.take_profit_percent,
                    trailing_distance_pct = NULL,
                    trailing_activation_pct = NULL,
                    updated_at = NOW()
            """
            db.execute_query(update_params_query, (
                current_user.id, False, sl_percent, tp_percent
            ))
            
            mode_text = f"Fixed TP/SL (TP: {tp_percent}%, SL: {sl_percent}%)"

        # Выполняем инициализацию
        result = initialize_signals_with_params(
            db,
            hours_back=hours_back,
            tp_percent=tp_percent,
            sl_percent=sl_percent
        )

        return jsonify({
            'status': 'success',
            'stats': result,
            'message': f"Система переинициализирована в режиме {mode_text}. "
                       f"Инициализировано {result['initialized']} сигналов. "
                       f"TP: {result['closed_tp']}, SL: {result['closed_sl']}, "
                       f"Открыто: {result['open']}"
        })

    except Exception as e:
        logger.error(f"Ошибка при инициализации: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/save_filters', methods=['POST'])
@login_required
def api_save_filters():
    """Сохранение настроек фильтров пользователя"""
    try:
        data = request.get_json()

        # Валидация
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
            allowed_hours = list(range(24))  # По умолчанию все часы
        # Фильтруем только валидные часы (0-23)
        allowed_hours = [h for h in allowed_hours if 0 <= h <= 23]

        # НЕ сохраняем TP/SL здесь - они меняются только через инициализацию
        upsert_query = """
            INSERT INTO web.user_signal_filters (
                user_id, hide_younger_than_hours, hide_older_than_hours,
                position_size_usd, leverage, score_week_min, score_month_min,
                allowed_hours, max_trades_per_15min
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                hide_younger_than_hours = EXCLUDED.hide_younger_than_hours,
                hide_older_than_hours = EXCLUDED.hide_older_than_hours,
                position_size_usd = EXCLUDED.position_size_usd,
                leverage = EXCLUDED.leverage,
                score_week_min = EXCLUDED.score_week_min,
                score_month_min = EXCLUDED.score_month_min,
                allowed_hours = EXCLUDED.allowed_hours,
                max_trades_per_15min = EXCLUDED.max_trades_per_15min,
                updated_at = NOW()
        """

        db.execute_query(upsert_query, (
            current_user.id, hide_younger, hide_older, position_size, leverage,
            score_week_min, score_month_min, allowed_hours, max_trades_per_15min
        ))

        return jsonify({
            'status': 'success',
            'message': 'Фильтры сохранены'
        })

    except Exception as e:
        logger.error(f"Ошибка при сохранении фильтров: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/scoring/get_date_info', methods=['POST'])
@login_required
def api_scoring_get_date_info():
    """API для получения информации о выбранной дате - УПРОЩЕННАЯ ВЕРСИЯ"""
    try:
        data = request.get_json()
        selected_date = data.get('date')
        score_week_min = data.get('score_week')
        score_month_min = data.get('score_month')

        from database import get_scoring_date_info

        # Получаем информацию через новую упрощенную функцию
        date_info = get_scoring_date_info(db, selected_date, score_week_min, score_month_min)

        return jsonify({
            'status': 'success',
            'date': selected_date,
            'market_regimes': date_info['market_regimes'],
            'dominant_regime': date_info['dominant_regime'],
            'signal_count': date_info['signal_count']
        })

    except Exception as e:
        logger.error(f"Ошибка получения информации о дате: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# Добавьте эти endpoints в app.py после существующих API routes

@app.route('/api/initialize_signals_trailing', methods=['POST'])
@login_required
def api_initialize_signals_trailing():
    """Инициализация с поддержкой Trailing Stop"""
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Только для администраторов'}), 403

    try:
        data = request.get_json() or {}

        # Получаем параметры
        hours_back = data.get('hours', 48)
        use_trailing_stop = data.get('use_trailing_stop', False)

        # Сохраняем настройки пользователя
        if use_trailing_stop:
            # Режим Trailing Stop
            trailing_distance = data.get('trailing_distance', 2.0)
            trailing_activation = data.get('trailing_activation', 1.0)
            insurance_sl = data.get('insurance_sl', 3.0)

            update_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, use_trailing_stop, 
                    trailing_distance_pct, trailing_activation_pct,
                    stop_loss_percent
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    use_trailing_stop = EXCLUDED.use_trailing_stop,
                    trailing_distance_pct = EXCLUDED.trailing_distance_pct,
                    trailing_activation_pct = EXCLUDED.trailing_activation_pct,
                    stop_loss_percent = EXCLUDED.stop_loss_percent,
                    updated_at = NOW()
            """
            db.execute_query(update_query, (
                current_user.id, True, trailing_distance,
                trailing_activation, insurance_sl
            ))

            mode_text = f"Trailing Stop (активация: {trailing_activation}%, дистанция: {trailing_distance}%)"
        else:
            # Режим Fixed TP/SL
            tp_percent = data.get('take_profit', 4.0)
            sl_percent = data.get('stop_loss', 3.0)

            update_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, use_trailing_stop,
                    stop_loss_percent, take_profit_percent
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    use_trailing_stop = EXCLUDED.use_trailing_stop,
                    stop_loss_percent = EXCLUDED.stop_loss_percent,
                    take_profit_percent = EXCLUDED.take_profit_percent,
                    updated_at = NOW()
            """
            db.execute_query(update_query, (
                current_user.id, False, sl_percent, tp_percent
            ))

            mode_text = f"Fixed TP/SL (TP: {tp_percent}%, SL: {sl_percent}%)"

        # Переинициализация сигналов
        from database import initialize_signals_with_trailing
        result = initialize_signals_with_trailing(
            db, hours_back=hours_back, user_id=current_user.id
        )

        return jsonify({
            'status': 'success',
            'stats': result,
            'message': f"Инициализировано {result['initialized']} сигналов в режиме {mode_text}"
        })

    except Exception as e:
        logger.error(f"Ошибка при инициализации с trailing: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/get_user_trading_mode')
@login_required
def api_get_user_trading_mode():
    """Получение текущего режима торговли пользователя"""
    try:
        query = """
            SELECT use_trailing_stop, trailing_distance_pct, 
                   trailing_activation_pct, stop_loss_percent,
                   take_profit_percent, position_size_usd, leverage,
                   allowed_hours
            FROM web.user_signal_filters
            WHERE user_id = %s
        """
        result = db.execute_query(query, (current_user.id,), fetch=True)

        if result:
            data = result[0]
            return jsonify({
                'use_trailing_stop': data['use_trailing_stop'] or False,
                'trailing_distance_pct': float(data['trailing_distance_pct'] or 2.0),
                'trailing_activation_pct': float(data['trailing_activation_pct'] or 1.0),
                'stop_loss': float(data['stop_loss_percent'] or 3.0),  # Для совместимости с JS
                'stop_loss_percent': float(data['stop_loss_percent'] or 3.0),
                'insurance_sl': float(data['stop_loss_percent'] or 3.0),
                'take_profit_percent': float(data['take_profit_percent'] or 4.0),
                'position_size': float(data['position_size_usd'] or 100.0),
                'leverage': int(data['leverage'] or 5),
                'allowed_hours': data.get('allowed_hours', list(range(24)))
            })
        else:
            return jsonify({
                'use_trailing_stop': False,
                'trailing_distance_pct': 2.0,
                'trailing_activation_pct': 1.0,
                'stop_loss': 3.0,
                'stop_loss_percent': 3.0,
                'insurance_sl': 3.0,
                'take_profit_percent': 4.0,
                'position_size': 100.0,
                'leverage': 5,
                'allowed_hours': list(range(24))
            })

    except Exception as e:
        logger.error(f"Ошибка получения режима торговли: {e}")
        return jsonify({'error': str(e)}), 500


# ========== АНАЛИЗ ЭФФЕКТИВНОСТИ ==========

@app.route('/efficiency_analysis')
@login_required
def efficiency_analysis():
    """Страница анализа эффективности"""
    return render_template('efficiency_analysis.html', 
                          username=current_user.username,
                          is_admin=current_user.is_admin)


@app.route('/tpsl_analysis')
@login_required  
def tpsl_analysis():
    """Страница анализа эффективности TP/SL"""
    return render_template('tpsl_analysis.html',
                          username=current_user.username,
                          is_admin=current_user.is_admin)


@app.route('/trailing_analysis')
@login_required
def trailing_analysis():
    """Страница анализа эффективности Trailing Stop"""
    return render_template('trailing_analysis.html',
                          username=current_user.username,
                          is_admin=current_user.is_admin)


@app.route('/api/efficiency/analyze_30days_progress')
@login_required
def api_efficiency_analyze_30days_progress():
    """SSE endpoint для отправки прогресса анализа эффективности в реальном времени"""
    from flask import Response, request
    from database import get_scoring_signals_v2, process_scoring_signals_batch
    from datetime import datetime, timedelta
    import uuid
    import json
    import time
    
    # Получаем параметры из запроса
    score_week_min_param = int(request.args.get('score_week_min', 60))
    score_week_max_param = int(request.args.get('score_week_max', 80))
    score_month_min_param = int(request.args.get('score_month_min', 60))
    score_month_max_param = int(request.args.get('score_month_max', 80))
    step_param = int(request.args.get('step', 10))
    max_trades_per_15min = int(request.args.get('max_trades_per_15min', 3))
    force_recalc = request.args.get('force_recalc', 'false').lower() == 'true'
    session_id = request.args.get('session_id', '')  # ID сессии для отслеживания переподключений
    
    # Сохраняем user_id до создания генератора
    user_id = current_user.id
    
    def generate():
        nonlocal session_id, force_recalc  # Указываем, что используем внешние переменные
        import gc  # Для ручной сборки мусора при больших объемах данных
        try:
            # Сначала вычисляем количество комбинаций (нужно для проверки прогресса)
            week_steps = list(range(score_week_min_param, score_week_max_param + 1, step_param))
            month_steps = list(range(score_month_min_param, score_month_max_param + 1, step_param))
            total_combinations = len(week_steps) * len(month_steps)
            
            # Проверяем, есть ли сохраненные промежуточные результаты в БД
            start_from_combination = 0
            results = []
            current_combination = 0
            
            if not force_recalc:
                # Проверяем прогресс в БД
                progress_query = """
                    SELECT parameters, last_processed_combination, results, completed
                    FROM web.analysis_progress
                    WHERE user_id = %s AND analysis_type = 'efficiency'
                    AND updated_at > NOW() - INTERVAL '24 hours'
                """
                progress_data = db.execute_query(progress_query, (user_id,), fetch=True)
                
                if progress_data:
                    saved_progress = progress_data[0]
                    saved_params = saved_progress['parameters']
                    
                    # Проверяем, что параметры совпадают
                    if (saved_params.get('score_week_min') == score_week_min_param and
                        saved_params.get('score_week_max') == score_week_max_param and
                        saved_params.get('score_month_min') == score_month_min_param and
                        saved_params.get('score_month_max') == score_month_max_param and
                        saved_params.get('step') == step_param and
                        saved_params.get('max_trades_per_15min') == max_trades_per_15min):
                        
                        # Продолжаем с последней обработанной комбинации
                        start_from_combination = saved_progress['last_processed_combination'] or 0
                        results = saved_progress.get('results', []) or []
                        
                        if not saved_progress['completed']:
                            # Логируем восстановление прогресса
                            logger.info(f'Восстанавливаем прогресс с комбинации {start_from_combination + 1} из {total_combinations}')
                            # ВАЖНО: отправляем клиенту информацию о восстановлении
                            yield f"data: {json.dumps({'type': 'info', 'message': f'Продолжаем с {start_from_combination + 1} из {total_combinations}'})}\n\n"
                    else:
                        # Параметры изменились, удаляем старый прогресс
                        db.execute_query("DELETE FROM web.analysis_progress WHERE user_id = %s AND analysis_type = 'efficiency'", (user_id,))
            else:
                # Принудительный пересчет - удаляем старый прогресс
                db.execute_query("DELETE FROM web.analysis_progress WHERE user_id = %s AND analysis_type = 'efficiency'", (user_id,))
                
                # При новом запуске очищаем весь кэш пользователя для пересчета
                if force_recalc:
                    clear_cache_query = """
                        DELETE FROM web.efficiency_cache 
                        WHERE user_id = %s
                    """
                    db.execute_query(clear_cache_query, (user_id,))
                else:
                    # Очищаем только старый кэш (старше 2 часов)
                    clear_old_cache_query = """
                        DELETE FROM web.efficiency_cache 
                        WHERE user_id = %s 
                        AND created_at < NOW() - INTERVAL '2 hours'
                    """
                    db.execute_query(clear_old_cache_query, (user_id,))
            
            # Отправляем немедленный heartbeat для проверки соединения
            # ВАЖНО: Отправляем простую строку сначала для проверки соединения
            yield ": ping\n\n"
            yield f"data: {json.dumps({'type': 'connection_test'})}\n\n"
            
            # Переменные для отслеживания времени и обработки
            last_heartbeat = time.time()
            last_yield = time.time()
            processed_combinations = 0
            HEARTBEAT_INTERVAL = 10  # Отправлять heartbeat каждые 10 секунд
            
            # Получаем настройки пользователя
            settings_query = """
                SELECT use_trailing_stop, trailing_distance_pct, trailing_activation_pct,
                       take_profit_percent, stop_loss_percent, position_size_usd, leverage,
                       allowed_hours
                FROM web.user_signal_filters
                WHERE user_id = %s
            """
            user_settings = db.execute_query(settings_query, (user_id,), fetch=True)
            
            if not user_settings:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Настройки пользователя не найдены'})}\n\n"
                return
            
            settings = user_settings[0]
            use_trailing_stop = settings.get('use_trailing_stop', False)
            trailing_distance_pct = float(settings.get('trailing_distance_pct', 2.0))
            trailing_activation_pct = float(settings.get('trailing_activation_pct', 1.0))
            tp_percent = float(settings.get('take_profit_percent', 4.0))
            sl_percent = float(settings.get('stop_loss_percent', 3.0))
            position_size = float(settings.get('position_size_usd', 100.0))
            leverage = int(settings.get('leverage', 5))
            allowed_hours = settings.get('allowed_hours', list(range(24)))
            if not allowed_hours:
                allowed_hours = list(range(24))
            
            # Определяем период анализа (исключаем последние 2 дня)
            end_date = datetime.now().date() - timedelta(days=2)
            start_date = end_date - timedelta(days=29)
            
            # Создаем или обновляем запись прогресса в БД
            if start_from_combination == 0:  # Новый анализ
                progress_params = {
                    'score_week_min': score_week_min_param,
                    'score_week_max': score_week_max_param,
                    'score_month_min': score_month_min_param,
                    'score_month_max': score_month_max_param,
                    'step': step_param,
                    'use_trailing_stop': use_trailing_stop,
                    'max_trades_per_15min': max_trades_per_15min
                }
                
                # Создаем новую запись прогресса
                upsert_query = """
                    INSERT INTO web.analysis_progress 
                    (user_id, analysis_type, parameters, total_combinations, results, last_processed_combination)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    ON CONFLICT (user_id, analysis_type) 
                    DO UPDATE SET 
                        parameters = EXCLUDED.parameters,
                        total_combinations = EXCLUDED.total_combinations,
                        results = EXCLUDED.results,
                        last_processed_combination = EXCLUDED.last_processed_combination,
                        completed = FALSE,
                        updated_at = NOW()
                """
                db.execute_query(upsert_query, (
                    user_id, 
                    'efficiency',
                    json.dumps(progress_params),
                    total_combinations,
                    json.dumps(results),
                    0
                ))
            
            # Отправляем начальное сообщение
            if start_from_combination > 0:
                # Показываем продолжение только если это действительно возобновление после длительного перерыва
                yield f"data: {json.dumps({'type': 'start', 'message': f'Анализ эффективности за 30 дней', 'total_combinations': total_combinations, 'resume': True, 'start_from': start_from_combination})}\n\n"
                # Отправляем уже обработанные результаты
                if results:
                    for result in results[:5]:  # Отправляем первые 5 для отображения
                        combination_str = f"Week≥{result['score_week']}%, Month≥{result['score_month']}%"
                        yield f"data: {json.dumps({'type': 'intermediate', 'combination': combination_str, 'pnl': round(result['total_pnl'], 2), 'signals': result['total_signals'], 'win_rate': round(result['win_rate'], 1)})}\n\n"
            else:
                # Отправляем начальное сообщение
                start_msg = {'type': 'start', 'message': 'Инициализация анализа за 30 дней...', 'total_combinations': total_combinations}
                yield f"data: {json.dumps(start_msg)}\n\n"
                # Форсируем отправку
                yield ": keepalive\n\n"
                
                # Сразу отправляем первый прогресс чтобы показать начало работы
                yield f"data: {json.dumps({'type': 'progress', 'percent': 0, 'message': 'Начинаем обработку...', 'current_combination': 0, 'total_combinations': total_combinations})}\n\n"
            
            # Перебираем все комбинации на основе пользовательских настроек
            for score_week_min in week_steps:
                for score_month_min in month_steps:
                    current_combination += 1
                    
                    # Пропускаем уже обработанные комбинации
                    if current_combination <= start_from_combination:
                        continue
                        
                    processed_combinations += 1
                    
                    # Отправляем heartbeat для поддержания соединения
                    current_time = time.time()
                    if current_time - last_heartbeat > HEARTBEAT_INTERVAL:
                        yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
                        last_heartbeat = current_time
                    
                    # Также отправляем keepalive чаще для предотвращения таймаута
                    if current_time - last_yield > 2:  # Каждые 2 секунды
                        yield f": keepalive\n\n"
                        last_yield = current_time
                    
                    combination_result = {
                        'score_week': score_week_min,
                        'score_month': score_month_min,
                        'start_date': start_date.strftime('%Y-%m-%d'),
                        'end_date': end_date.strftime('%Y-%m-%d'),
                        'total_pnl': 0.0,
                        'total_signals': 0,
                        'total_wins': 0,
                        'total_losses': 0,
                        'win_rate': 0.0,
                        'daily_breakdown': []
                    }
                    
                    # Вычисляем прогресс для текущей комбинации
                    progress_percent = int((current_combination - 0.5) / total_combinations * 100)
                    
                    # Отправляем прогресс в начале обработки каждой комбинации
                    progress_data = {
                        'type': 'progress', 
                        'percent': progress_percent, 
                        'message': f'Обработка {current_combination}/{total_combinations}', 
                        'current_combination': current_combination, 
                        'total_combinations': total_combinations
                    }
                    yield f"data: {json.dumps(progress_data)}\n\n"
                    last_yield = time.time()
                    
                    # Обрабатываем каждый день
                    current_date = start_date
                    days_processed = 0
                    
                    while current_date <= end_date:
                        days_processed += 1
                        date_str = current_date.strftime('%Y-%m-%d')
                        
                        # Отправляем heartbeat регулярно
                        current_time = time.time()
                        if current_time - last_heartbeat > HEARTBEAT_INTERVAL:
                            yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
                            last_heartbeat = current_time
                        
                        # Также отправляем keepalive чаще для предотвращения таймаута
                        if current_time - last_yield > 2:
                            yield f": keepalive\n\n"
                            last_yield = current_time
                        
                        # Не отправляем детальный прогресс для уменьшения нагрузки при большом количестве комбинаций
                        # Только для малого количества комбинаций
                        if total_combinations <= 50 and (days_processed % 10 == 0 or days_processed == 1 or days_processed == 30):
                            detail_progress = progress_percent + int((days_processed / 30) * (100 / total_combinations) * 0.9)
                            yield f"data: {json.dumps({'type': 'progress_detail', 'percent': detail_progress, 'message': f'Комбинация {current_combination}: день {days_processed}/30'})}\n\n"
                            last_yield = current_time
                        
                        # Проверяем кэш для этой комбинации и дня
                        cache_key = f"{date_str}_{score_week_min}_{score_month_min}_{use_trailing_stop}_{max_trades_per_15min}"
                        cached_result = None
                        
                        # Используем кэш только если не требуется принудительный пересчет и не используется trailing stop
                        # (кэш не содержит trailing_wins/trailing_losses)
                        if not force_recalc and not use_trailing_stop:
                            cache_query = """
                                SELECT signal_count, tp_count, sl_count, timeout_count, daily_pnl
                                FROM web.efficiency_cache
                                WHERE cache_key = %s AND user_id = %s
                                    AND created_at > NOW() - INTERVAL '1 hour'
                            """
                            cached_result = db.execute_query(cache_query, (cache_key, user_id), fetch=True)
                        
                        if cached_result:
                            # Используем кэшированные данные
                            daily_stats = {
                                'date': date_str,
                                'signal_count': cached_result[0]['signal_count'],
                                'tp_count': cached_result[0]['tp_count'],
                                'sl_count': cached_result[0]['sl_count'],
                                'trailing_wins': 0,  # Кэш не содержит эти поля, нужно пересчитать
                                'trailing_losses': 0,
                                'timeout_count': cached_result[0]['timeout_count'],
                                'daily_pnl': float(cached_result[0]['daily_pnl'])
                            }
                        else:
                            # Получаем сигналы для текущего дня с учетом разрешенных часов и лимита 15 минут
                            raw_signals = get_scoring_signals_v2(db, date_str, score_week_min, score_month_min, allowed_hours, max_trades_per_15min)
                            
                            daily_stats = {
                                'date': date_str,
                                'signal_count': 0,
                                'tp_count': 0,
                                'sl_count': 0,
                                'trailing_wins': 0,
                                'trailing_losses': 0,
                                'timeout_count': 0,
                                'daily_pnl': 0.0
                            }
                            
                            if raw_signals:
                                session_id = f"eff_{user_id}_{uuid.uuid4().hex[:8]}"
                                
                                # КРИТИЧНО: отправляем heartbeat перед КАЖДОЙ тяжелой операцией
                                # при большом количестве комбинаций
                                if total_combinations > 100:
                                    yield f": processing c{current_combination}d{days_processed}\n\n"
                                
                                result = process_scoring_signals_batch(
                                    db, raw_signals, session_id, user_id,
                                    tp_percent=tp_percent,
                                    sl_percent=sl_percent,
                                    position_size=position_size,
                                    leverage=leverage,
                                    use_trailing_stop=use_trailing_stop,
                                    trailing_distance_pct=trailing_distance_pct,
                                    trailing_activation_pct=trailing_activation_pct
                                )
                            
                                stats = result['stats']
                                daily_stats['signal_count'] = int(stats.get('total', 0))
                                daily_stats['tp_count'] = int(stats.get('tp_count', 0))
                                daily_stats['sl_count'] = int(stats.get('sl_count', 0))
                                daily_stats['timeout_count'] = int(stats.get('timeout_count', 0))
                                daily_stats['daily_pnl'] = float(stats.get('total_pnl', 0))
                                
                                # Для trailing stop стратегии учитываем trailing_wins и trailing_losses
                                if use_trailing_stop:
                                    daily_stats['trailing_wins'] = int(stats.get('trailing_wins', 0))
                                    daily_stats['trailing_losses'] = int(stats.get('trailing_losses', 0))
                                
                                # Сохраняем в кэш только для Fixed режима
                                if not use_trailing_stop:
                                    cache_insert = """
                                        INSERT INTO web.efficiency_cache 
                                        (cache_key, user_id, signal_count, tp_count, sl_count, timeout_count, daily_pnl)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                                    """
                                    try:
                                        db.execute_query(cache_insert, (
                                            cache_key, user_id,
                                            daily_stats['signal_count'],
                                            daily_stats['tp_count'],
                                            daily_stats['sl_count'],
                                            daily_stats['timeout_count'],
                                            daily_stats['daily_pnl']
                                        ))
                                    except:
                                        pass  # Игнорируем ошибки кэша
                                
                                # Очищаем временные данные из правильной таблицы
                                cleanup_query = """
                                    DELETE FROM web.scoring_analysis_results
                                    WHERE session_id = %s AND user_id = %s
                                """
                                # Очищаем после обработки дня
                                db.execute_query(cleanup_query, (session_id, user_id))
                        
                        # Обновляем общую статистику
                        if daily_stats:
                            combination_result['total_signals'] += daily_stats['signal_count']
                            combination_result['total_pnl'] += daily_stats['daily_pnl']
                            
                            # Правильно считаем wins и losses с учетом trailing
                            if use_trailing_stop:
                                total_wins = daily_stats.get('tp_count', 0) + daily_stats.get('trailing_wins', 0)
                                total_losses = daily_stats.get('sl_count', 0) + daily_stats.get('trailing_losses', 0)
                            else:
                                total_wins = daily_stats.get('tp_count', 0)
                                total_losses = daily_stats.get('sl_count', 0)
                            
                            combination_result['total_wins'] += total_wins
                            combination_result['total_losses'] += total_losses
                        
                        combination_result['daily_breakdown'].append(daily_stats)
                        current_date += timedelta(days=1)
                        
                        # Отправляем heartbeat после каждого дня для поддержания соединения
                        if days_processed % 5 == 0:  # Каждые 5 дней
                            yield f": heartbeat day {days_processed}\n\n"
                            last_yield = time.time()
                    
                    # Рассчитываем win rate
                    if combination_result['total_signals'] > 0:
                        total_closed = combination_result['total_wins'] + combination_result['total_losses']
                        if total_closed > 0:
                            combination_result['win_rate'] = (combination_result['total_wins'] / total_closed) * 100
                        
                        results.append(combination_result)
                        
                        # Очищаем память при большом количестве результатов
                        # Оставляем только топ-200 по win_rate чтобы не исчерпать память
                        if len(results) > 200 and total_combinations > 100:
                            # Сортируем и оставляем только лучшие
                            results = sorted(results, key=lambda x: x.get('win_rate', 0), reverse=True)[:200]
                            logger.info(f"Очищена память, оставлено топ-200 результатов из {current_combination}")
                            # Принудительная сборка мусора
                            gc.collect()
                        
                        # КРИТИЧНО: При большом количестве комбинаций сохраняем КАЖДУЮ
                        # чтобы не потерять прогресс при разрыве соединения
                        should_save = False
                        
                        if total_combinations > 100:
                            # Сохраняем каждую комбинацию
                            should_save = True
                        else:
                            # При малом количестве сохраняем каждую 5-ю
                            should_save = (current_combination % 5 == 0) or (current_combination == total_combinations)
                        
                        # ВСЕГДА сохраняем первую обработанную комбинацию после восстановления
                        if current_combination == start_from_combination + 1:
                            should_save = True
                        
                        if should_save:
                            update_progress_query = """
                                UPDATE web.analysis_progress 
                                SET results = %s, 
                                    last_processed_combination = %s,
                                    updated_at = NOW()
                                WHERE user_id = %s AND analysis_type = 'efficiency'
                            """
                            try:
                                db.execute_query(update_progress_query, (
                                    json.dumps(results),
                                    current_combination,
                                    user_id
                                ))
                                # Отправляем keepalive после сохранения
                                yield f": saved {current_combination}\n\n"
                            except Exception as e:
                                logger.error(f"Не удалось сохранить прогресс на комбинации {current_combination}: {e}")
                                # Продолжаем работу даже если не удалось сохранить
                        
                        # Отправляем промежуточный результат только для малого количества комбинаций
                        if total_combinations <= 50:
                            yield f"data: {json.dumps({'type': 'intermediate', 'combination': f'Week≥{score_week_min}%, Month≥{score_month_min}%', 'pnl': round(combination_result['total_pnl'], 2), 'signals': combination_result['total_signals'], 'win_rate': round(combination_result['win_rate'], 1)})}\n\n"
                            last_yield = time.time()
            
            # Сортируем результаты по Win Rate
            results.sort(key=lambda x: x['win_rate'], reverse=True)
            
            # Сохраняем финальные результаты в БД и помечаем как завершенные
            final_update_query = """
                UPDATE web.analysis_progress 
                SET results = %s, 
                    last_processed_combination = %s,
                    completed = TRUE,
                    updated_at = NOW()
                WHERE user_id = %s AND analysis_type = 'efficiency'
            """
            db.execute_query(final_update_query, (
                json.dumps(results),
                total_combinations,
                user_id
            ))
            
            # Также сохраняем в памяти для быстрого доступа в текущей сессии
            if 'efficiency' not in analysis_results_cache:
                analysis_results_cache['efficiency'] = {}
                
            analysis_results_cache['efficiency'][user_id] = {
                'timestamp': datetime.now().isoformat(),
                'results': results,
                'parameters': {
                    'score_week_min': score_week_min_param,
                    'score_week_max': score_week_max_param,
                    'score_month_min': score_month_min_param,
                    'score_month_max': score_month_max_param,
                    'step': step_param,
                    'use_trailing_stop': use_trailing_stop,
                    'tp_percent': tp_percent,
                    'sl_percent': sl_percent,
                    'position_size': position_size,
                    'leverage': leverage
                },
                'completed': True
            }
            
            # Отправляем финальные результаты
            yield f"data: {json.dumps({'type': 'complete', 'data': results})}\n\n"
            
        except Exception as e:
            logger.error(f"Ошибка в SSE генераторе: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    # Создаем Response БЕЗ stream_with_context - это может блокировать
    response = Response(
        generate(),  # Простой генератор без оберток
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',  # Отключает буферизацию в Nginx
        }
    )
    
    return response


@app.route('/api/tpsl/analyze_progress')
@login_required
def api_tpsl_analyze_progress():
    """SSE endpoint для анализа эффективности TP/SL"""
    from flask import Response, request
    from database import get_scoring_signals_v2, process_scoring_signals_batch
    from datetime import datetime, timedelta
    import uuid
    import json
    import time
    
    # Получаем параметры из запроса
    score_week = int(request.args.get('score_week', 70))
    score_month = int(request.args.get('score_month', 70))
    tp_min = float(request.args.get('tp_min', 2.0))
    tp_max = float(request.args.get('tp_max', 6.0))
    sl_min = float(request.args.get('sl_min', 1.0))
    sl_max = float(request.args.get('sl_max', 4.0))
    step = float(request.args.get('step', 0.5))
    max_trades_per_15min = int(request.args.get('max_trades_per_15min', 3))
    
    # Сохраняем user_id до создания генератора
    user_id = current_user.id
    
    def generate():
        try:
            # Очищаем предыдущие результаты анализа TP/SL для этого пользователя
            if user_id in analysis_results_cache['tp_sl']:
                del analysis_results_cache['tp_sl'][user_id]
            
            # Отправляем немедленный heartbeat
            yield f": heartbeat\n\n"
            yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            
            # Переменные для отслеживания
            last_heartbeat = time.time()
            last_yield = time.time()
            
            # Получаем настройки пользователя (позиция, плечо и разрешенные часы)
            settings_query = """
                SELECT position_size_usd, leverage, allowed_hours
                FROM web.user_signal_filters
                WHERE user_id = %s
            """
            user_settings = db.execute_query(settings_query, (user_id,), fetch=True)
            
            if not user_settings:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Настройки пользователя не найдены'})}\n\n"
                return
            
            settings = user_settings[0]
            position_size = float(settings.get('position_size_usd', 100.0))
            leverage = int(settings.get('leverage', 5))
            allowed_hours = settings.get('allowed_hours', list(range(24)))
            if not allowed_hours:
                allowed_hours = list(range(24))
            
            # Определяем период анализа (исключаем последние 2 дня)
            end_date = datetime.now().date() - timedelta(days=2)
            start_date = end_date - timedelta(days=29)
            
            # Генерируем комбинации TP/SL
            tp_values = []
            sl_values = []
            current = tp_min
            while current <= tp_max:
                tp_values.append(round(current, 1))
                current += step
            
            current = sl_min  
            while current <= sl_max:
                sl_values.append(round(current, 1))
                current += step
            
            total_combinations = len(tp_values) * len(sl_values)
            current_combination = 0
            
            # Отправляем начальное сообщение
            yield f"data: {json.dumps({'type': 'start', 'message': f'Анализ TP/SL для Score Week≥{score_week}%, Score Month≥{score_month}%', 'total_combinations': total_combinations})}\n\n"
            
            results = []
            
            # Перебираем все комбинации TP/SL
            for tp_percent in tp_values:
                for sl_percent in sl_values:
                    current_combination += 1
                    
                    # Отправляем прогресс каждые 5 комбинаций или в конце
                    if current_combination % 5 == 1 or current_combination == total_combinations:
                        progress_percent = int((current_combination / total_combinations) * 100)
                        yield f"data: {json.dumps({'type': 'progress', 'percent': progress_percent, 'message': f'Анализ TP: {tp_percent}%, SL: {sl_percent}%', 'details': f'Комбинация {current_combination} из {total_combinations}'})}\n\n"
                        last_yield = time.time()
                    
                    combination_result = {
                        'tp': tp_percent,
                        'sl': sl_percent,
                        'start_date': start_date.strftime('%Y-%m-%d'),
                        'end_date': end_date.strftime('%Y-%m-%d'),
                        'total_pnl': 0.0,
                        'total_signals': 0,
                        'total_wins': 0,
                        'total_losses': 0,
                        'win_rate': 0.0,
                        'daily_breakdown': []
                    }
                    
                    # Обрабатываем каждый день
                    current_date = start_date
                    while current_date <= end_date:
                        date_str = current_date.strftime('%Y-%m-%d')
                        
                        # Отправляем keepalive более активно для предотвращения таймаута
                        current_time = time.time()
                        if current_time - last_yield > 2:  # Уменьшаем с 3 до 2 секунд
                            yield f": keepalive\n\n"
                            yield f"data: {json.dumps({'type': 'heartbeat', 'timestamp': current_time})}\n\n"
                            last_yield = current_time
                        
                        # Проверяем кэш
                        cache_key = f"tpsl_{date_str}_{score_week}_{score_month}_{tp_percent}_{sl_percent}_{max_trades_per_15min}"
                        cache_query = """
                            SELECT signal_count, tp_count, sl_count, timeout_count, daily_pnl
                            FROM web.efficiency_cache
                            WHERE cache_key = %s AND user_id = %s
                                AND created_at > NOW() - INTERVAL '1 hour'
                        """
                        cached_result = db.execute_query(cache_query, (cache_key, user_id), fetch=True)
                        
                        if cached_result:
                            daily_stats = {
                                'date': date_str,
                                'signal_count': cached_result[0]['signal_count'],
                                'tp_count': cached_result[0]['tp_count'],
                                'sl_count': cached_result[0]['sl_count'],
                                'timeout_count': cached_result[0]['timeout_count'],
                                'daily_pnl': float(cached_result[0]['daily_pnl'])
                            }
                        else:
                            # Получаем сигналы с фильтрами Score Week, Score Month и разрешенными часами
                            raw_signals = get_scoring_signals_v2(db, date_str, score_week, score_month, allowed_hours, max_trades_per_15min)
                            
                            daily_stats = {
                                'date': date_str,
                                'signal_count': 0,
                                'tp_count': 0,
                                'sl_count': 0,
                                'trailing_wins': 0,
                                'trailing_losses': 0,
                                'timeout_count': 0,
                                'daily_pnl': 0.0
                            }
                            
                            if raw_signals:
                                session_id = f"tpsl_{user_id}_{uuid.uuid4().hex[:8]}"
                                
                                # Отправляем heartbeat перед длительной обработкой
                                current_time = time.time()
                                if current_time - last_yield > 1.5:
                                    yield f": processing\n\n"
                                    yield f"data: {json.dumps({'type': 'heartbeat', 'processing': date_str})}\n\n"
                                    last_yield = current_time
                                
                                # Обрабатываем с текущими TP/SL
                                result = process_scoring_signals_batch(
                                    db, raw_signals, session_id, user_id,
                                    tp_percent=tp_percent,
                                    sl_percent=sl_percent,
                                    position_size=position_size,
                                    leverage=leverage,
                                    use_trailing_stop=False  # Для анализа TP/SL используем Fixed режим
                                )
                                
                                stats = result['stats']
                                daily_stats['signal_count'] = int(stats.get('total', 0))
                                daily_stats['tp_count'] = int(stats.get('tp_count', 0))
                                daily_stats['sl_count'] = int(stats.get('sl_count', 0))
                                daily_stats['timeout_count'] = int(stats.get('timeout_count', 0))
                                daily_stats['daily_pnl'] = float(stats.get('total_pnl', 0))
                                
                                # Сохраняем в кэш
                                cache_insert = """
                                    INSERT INTO web.efficiency_cache 
                                    (cache_key, user_id, signal_count, tp_count, sl_count, timeout_count, daily_pnl)
                                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                                """
                                try:
                                    db.execute_query(cache_insert, (
                                        cache_key, user_id,
                                        daily_stats['signal_count'],
                                        daily_stats['tp_count'],
                                        daily_stats['sl_count'],
                                        daily_stats['timeout_count'],
                                        daily_stats['daily_pnl']
                                    ))
                                except:
                                    pass
                                
                                # Очищаем временные данные из правильной таблицы
                                cleanup_query = """
                                    DELETE FROM web.scoring_analysis_results
                                    WHERE session_id = %s AND user_id = %s
                                """
                                # Очищаем после обработки дня
                                db.execute_query(cleanup_query, (session_id, user_id))
                        
                        # Обновляем статистику
                        combination_result['total_signals'] += daily_stats['signal_count']
                        combination_result['total_pnl'] += daily_stats['daily_pnl']
                        combination_result['total_wins'] += daily_stats.get('tp_count', 0)
                        combination_result['total_losses'] += daily_stats.get('sl_count', 0)
                        combination_result['daily_breakdown'].append(daily_stats)
                        
                        current_date += timedelta(days=1)
                    
                    # Рассчитываем win rate
                    if combination_result['total_signals'] > 0:
                        total_closed = combination_result['total_wins'] + combination_result['total_losses']
                        if total_closed > 0:
                            combination_result['win_rate'] = (combination_result['total_wins'] / total_closed) * 100
                        
                        results.append(combination_result)
            
            # Сортируем по P&L
            results.sort(key=lambda x: x['total_pnl'], reverse=True)
            
            # Сохраняем результаты в глобальный кэш
            analysis_results_cache['tp_sl'][user_id] = {
                'timestamp': datetime.now().isoformat(),
                'results': results,
                'parameters': {
                    'score_week': score_week,
                    'score_month': score_month,
                    'tp_min': tp_min,
                    'tp_max': tp_max,
                    'sl_min': sl_min,
                    'sl_max': sl_max,
                    'step': step,
                    'position_size': position_size,
                    'leverage': leverage
                }
            }
            
            # Отправляем результаты
            yield f"data: {json.dumps({'type': 'complete', 'data': results})}\n\n"
            
        except Exception as e:
            logger.error(f"Ошибка в SSE генераторе TP/SL: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/trailing/analyze_progress')
@login_required
def api_trailing_analyze_progress():
    """SSE endpoint для анализа эффективности Trailing Stop"""
    from flask import Response, request
    from database import get_scoring_signals_v2, process_scoring_signals_batch
    from datetime import datetime, timedelta
    import uuid
    import json
    import time
    
    # Получаем параметры из запроса
    score_week = int(request.args.get('score_week', 70))
    score_month = int(request.args.get('score_month', 70))
    activation_min = float(request.args.get('activation_min', 0.5))
    activation_max = float(request.args.get('activation_max', 3.0))
    distance_min = float(request.args.get('distance_min', 0.5))
    distance_max = float(request.args.get('distance_max', 3.0))
    stop_loss_min = float(request.args.get('stop_loss_min', 1.0))
    stop_loss_max = float(request.args.get('stop_loss_max', 5.0))
    step = float(request.args.get('step', 0.5))
    max_trades_per_15min = int(request.args.get('max_trades_per_15min', 3))
    
    # Сохраняем user_id до создания генератора
    user_id = current_user.id
    
    def generate():
        try:
            # Очищаем предыдущие результаты анализа Trailing Stop для этого пользователя
            if user_id in analysis_results_cache['trailing']:
                del analysis_results_cache['trailing'][user_id]
            
            # Отправляем немедленный heartbeat
            yield f": heartbeat\n\n"
            yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
            
            # Переменные для отслеживания
            last_heartbeat = time.time()
            last_yield = time.time()
            
            # Получаем настройки пользователя (позиция, плечо и разрешенные часы)
            settings_query = """
                SELECT position_size_usd, leverage, take_profit_percent, allowed_hours
                FROM web.user_signal_filters
                WHERE user_id = %s
            """
            user_settings = db.execute_query(settings_query, (user_id,), fetch=True)
            
            if not user_settings:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Настройки пользователя не найдены'})}\n\n"
                return
            
            settings = user_settings[0]
            position_size = float(settings.get('position_size_usd', 100.0))
            leverage = int(settings.get('leverage', 5))
            # TP для Trailing Stop берем из настроек как максимальный уровень
            tp_percent = float(settings.get('take_profit_percent', 10.0))
            allowed_hours = settings.get('allowed_hours', list(range(24)))
            if not allowed_hours:
                allowed_hours = list(range(24))
            
            # Определяем период анализа (исключаем последние 2 дня)
            end_date = datetime.now().date() - timedelta(days=2)
            start_date = end_date - timedelta(days=29)
            
            # Генерируем комбинации Trailing параметров
            activation_values = []
            distance_values = []
            stop_loss_values = []
            
            current = activation_min
            while current <= activation_max:
                activation_values.append(round(current, 1))
                current += step
            
            current = distance_min  
            while current <= distance_max:
                distance_values.append(round(current, 1))
                current += step
            
            current = stop_loss_min
            while current <= stop_loss_max:
                stop_loss_values.append(round(current, 1))
                current += step
            
            total_combinations = len(activation_values) * len(distance_values) * len(stop_loss_values)
            current_combination = 0
            
            # Отправляем начальное сообщение
            yield f"data: {json.dumps({'type': 'start', 'message': f'Анализ Trailing Stop для Score Week≥{score_week}%, Score Month≥{score_month}%', 'total_combinations': total_combinations})}\n\n"
            
            results = []
            
            # Перебираем все комбинации Trailing параметров
            for activation_pct in activation_values:
                for distance_pct in distance_values:
                    for stop_loss in stop_loss_values:
                        current_combination += 1
                        
                        # Отправляем прогресс каждые 10 комбинаций или в конце
                        if current_combination % 10 == 1 or current_combination == total_combinations:
                            progress_percent = int((current_combination / total_combinations) * 100)
                            yield f"data: {json.dumps({'type': 'progress', 'percent': progress_percent, 'message': f'Анализ Act: {activation_pct}%, Dist: {distance_pct}%, SL: {stop_loss}%', 'details': f'Комбинация {current_combination} из {total_combinations}'})}\n\n"
                            last_yield = time.time()
                        
                        combination_result = {
                            'activation': activation_pct,
                            'distance': distance_pct,
                            'stop_loss': stop_loss,
                            'start_date': start_date.strftime('%Y-%m-%d'),
                            'end_date': end_date.strftime('%Y-%m-%d'),
                            'total_pnl': 0.0,
                            'total_signals': 0,
                            'total_wins': 0,
                            'total_losses': 0,
                            'trailing_count': 0,
                            'win_rate': 0.0,
                            'daily_breakdown': []
                        }
                        
                        # Обрабатываем каждый день
                        current_date = start_date
                        while current_date <= end_date:
                            date_str = current_date.strftime('%Y-%m-%d')
                            
                            # Отправляем keepalive если давно не отправляли данные
                            current_time = time.time()
                            if current_time - last_yield > 3:
                                yield f": keepalive\n\n"
                                last_yield = current_time
                            
                            # Проверяем кэш
                            cache_key = f"trail_{date_str}_{score_week}_{score_month}_{activation_pct}_{distance_pct}_{stop_loss}_{max_trades_per_15min}"
                            cache_query = """
                                SELECT signal_count, tp_count, sl_count, timeout_count, daily_pnl
                                FROM web.efficiency_cache
                                WHERE cache_key = %s AND user_id = %s
                                    AND created_at > NOW() - INTERVAL '1 hour'
                            """
                            cached_result = db.execute_query(cache_query, (cache_key, user_id), fetch=True)
                            
                            if cached_result:
                                daily_stats = {
                                    'date': date_str,
                                    'signal_count': cached_result[0]['signal_count'],
                                    'tp_count': cached_result[0]['tp_count'],
                                    'trailing_count': cached_result[0].get('tp_count', 0),  # В кэше trailing записан как tp_count
                                    'trailing_wins': 0,  # Кэш не содержит trailing_wins, пересчитаем
                                    'trailing_losses': 0,  # Кэш не содержит trailing_losses, пересчитаем
                                    'sl_count': cached_result[0]['sl_count'],
                                    'timeout_count': cached_result[0]['timeout_count'],
                                    'daily_pnl': float(cached_result[0]['daily_pnl'])
                                }
                            else:
                                # Получаем сигналы с фильтрами Score Week, Score Month и разрешенными часами
                                raw_signals = get_scoring_signals_v2(db, date_str, score_week, score_month, allowed_hours, max_trades_per_15min)
                                
                                daily_stats = {
                                    'date': date_str,
                                    'signal_count': 0,
                                    'tp_count': 0,
                                    'trailing_count': 0,
                                    'trailing_wins': 0,
                                    'trailing_losses': 0,
                                    'sl_count': 0,
                                    'timeout_count': 0,
                                    'daily_pnl': 0.0
                                }
                                
                                if raw_signals:
                                    session_id = f"trail_{user_id}_{uuid.uuid4().hex[:8]}"
                                    
                                    # Обрабатываем с Trailing Stop
                                    result = process_scoring_signals_batch(
                                        db, raw_signals, session_id, user_id,
                                        tp_percent=tp_percent,  # Максимальный TP
                                        sl_percent=stop_loss,
                                        position_size=position_size,
                                        leverage=leverage,
                                        use_trailing_stop=True,
                                        trailing_activation_pct=activation_pct,
                                        trailing_distance_pct=distance_pct
                                    )
                                    
                                    stats = result['stats']
                                    daily_stats['signal_count'] = int(stats.get('total', 0))
                                    daily_stats['tp_count'] = int(stats.get('tp_count', 0))
                                    daily_stats['trailing_count'] = int(stats.get('trailing_count', 0))
                                    daily_stats['trailing_wins'] = int(stats.get('trailing_wins', 0))
                                    daily_stats['trailing_losses'] = int(stats.get('trailing_losses', 0))
                                    daily_stats['sl_count'] = int(stats.get('sl_count', 0))
                                    daily_stats['timeout_count'] = int(stats.get('timeout_count', 0))
                                    daily_stats['daily_pnl'] = float(stats.get('total_pnl', 0))
                                    
                                    # Сохраняем в кэш
                                    cache_insert = """
                                        INSERT INTO web.efficiency_cache 
                                        (cache_key, user_id, signal_count, tp_count, sl_count, timeout_count, daily_pnl)
                                        VALUES (%s, %s, %s, %s, %s, %s, %s)
                                    """
                                    try:
                                        db.execute_query(cache_insert, (
                                            cache_key, user_id,
                                            daily_stats['signal_count'],
                                            daily_stats['trailing_count'],  # Сохраняем trailing_count для trailing stop анализа
                                            daily_stats['sl_count'],
                                            daily_stats['timeout_count'],
                                            daily_stats['daily_pnl']
                                        ))
                                    except:
                                        pass
                                    
                                    # Очищаем временные данные из правильной таблицы
                                    cleanup_query = """
                                        DELETE FROM web.scoring_analysis_results
                                        WHERE session_id = %s AND user_id = %s
                                    """
                                    # Очищаем после обработки дня
                                    db.execute_query(cleanup_query, (session_id, user_id))
                            
                            # Обновляем статистику
                            combination_result['total_signals'] += daily_stats['signal_count']
                            combination_result['total_pnl'] += daily_stats['daily_pnl']
                            
                            # Правильно считаем wins и losses для trailing stop
                            tp_wins = daily_stats.get('tp_count', 0)
                            trailing_wins = daily_stats.get('trailing_wins', 0)
                            sl_losses = daily_stats.get('sl_count', 0)
                            trailing_losses = daily_stats.get('trailing_losses', 0)
                            
                            combination_result['total_wins'] += tp_wins + trailing_wins
                            combination_result['trailing_count'] += daily_stats.get('trailing_count', 0)
                            combination_result['total_losses'] += sl_losses + trailing_losses
                            combination_result['daily_breakdown'].append(daily_stats)
                            
                            current_date += timedelta(days=1)
                        
                        # Рассчитываем win rate
                        if combination_result['total_signals'] > 0:
                            total_closed = combination_result['total_wins'] + combination_result['total_losses']
                            if total_closed > 0:
                                combination_result['win_rate'] = (combination_result['total_wins'] / total_closed) * 100
                            
                            results.append(combination_result)
            
            # Сортируем по P&L
            results.sort(key=lambda x: x['total_pnl'], reverse=True)
            
            # Сохраняем результаты в глобальный кэш
            analysis_results_cache['trailing'][user_id] = {
                'timestamp': datetime.now().isoformat(),
                'results': results,
                'parameters': {
                    'score_week': score_week,
                    'score_month': score_month,
                    'activation_min': activation_min,
                    'activation_max': activation_max,
                    'distance_min': distance_min,
                    'distance_max': distance_max,
                    'stop_loss': stop_loss,
                    'step': step,
                    'position_size': position_size,
                    'leverage': leverage
                }
            }
            
            # Отправляем результаты
            yield f"data: {json.dumps({'type': 'complete', 'data': results})}\n\n"
            
        except Exception as e:
            logger.error(f"Ошибка в SSE генераторе Trailing: {e}")
            import traceback
            logger.error(traceback.format_exc())
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no'
        }
    )


@app.route('/api/efficiency/analyze_30days', methods=['POST'])
@login_required
def api_efficiency_analyze_30days():
    """API для анализа эффективности за 30 дней с различными комбинациями фильтров score_week/score_month"""
    try:
        from database import get_scoring_signals, process_scoring_signals_batch, get_scoring_analysis_results
        from datetime import datetime, timedelta
        import uuid
        
        # Получаем настройки пользователя (TP/SL и режим торговли)
        settings_query = """
            SELECT use_trailing_stop, trailing_distance_pct, trailing_activation_pct,
                   take_profit_percent, stop_loss_percent, position_size_usd, leverage
            FROM web.user_signal_filters
            WHERE user_id = %s
        """
        user_settings = db.execute_query(settings_query, (current_user.id,), fetch=True)
        
        if not user_settings:
            return jsonify({
                'status': 'error',
                'message': 'Настройки пользователя не найдены'
            }), 400
        
        settings = user_settings[0]
        use_trailing_stop = settings.get('use_trailing_stop', False)
        trailing_distance_pct = float(settings.get('trailing_distance_pct', 2.0))
        trailing_activation_pct = float(settings.get('trailing_activation_pct', 1.0))
        tp_percent = float(settings.get('take_profit_percent', 4.0))
        sl_percent = float(settings.get('stop_loss_percent', 3.0))
        position_size = float(settings.get('position_size_usd', 100.0))
        leverage = int(settings.get('leverage', 5))
        
        # Определяем период анализа (последние 30 дней)
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=29)
        
        results = []
        
        # Перебираем все комбинации score_week и score_month от 60% до 90% с шагом 10%
        for score_week_min in range(60, 91, 10):
            for score_month_min in range(60, 91, 10):
                
                combination_result = {
                    'score_week': score_week_min,
                    'score_month': score_month_min,
                    'start_date': start_date.strftime('%Y-%m-%d'),
                    'end_date': end_date.strftime('%Y-%m-%d'),
                    'total_pnl': 0.0,
                    'total_signals': 0,
                    'total_wins': 0,
                    'total_losses': 0,
                    'win_rate': 0.0,
                    'daily_breakdown': []
                }
                
                # Обрабатываем каждый день в периоде
                current_date = start_date
                while current_date <= end_date:
                    date_str = current_date.strftime('%Y-%m-%d')
                    
                    # Получаем сигналы для текущего дня с фильтрами
                    raw_signals = get_scoring_signals(db, date_str, score_week_min, score_month_min)
                    
                    daily_stats = {
                        'date': date_str,
                        'signal_count': 0,
                        'tp_count': 0,
                        'sl_count': 0,
                        'trailing_wins': 0,
                        'trailing_losses': 0,
                        'timeout_count': 0,
                        'daily_pnl': 0.0
                    }
                    
                    if raw_signals:
                        # Генерируем уникальный session_id для этого расчета
                        session_id = f"eff_{current_user.id}_{uuid.uuid4().hex[:8]}"
                        
                        # Обрабатываем сигналы с учетом режима торговли
                        result = process_scoring_signals_batch(
                            db, raw_signals, session_id, current_user.id,
                            tp_percent=tp_percent,
                            sl_percent=sl_percent,
                            position_size=position_size,
                            leverage=leverage,
                            use_trailing_stop=use_trailing_stop,
                            trailing_distance_pct=trailing_distance_pct,
                            trailing_activation_pct=trailing_activation_pct
                        )
                        
                        # Получаем статистику из результата
                        stats = result['stats']
                        daily_stats['signal_count'] = int(stats.get('total', 0))
                        daily_stats['tp_count'] = int(stats.get('tp_count', 0))
                        daily_stats['sl_count'] = int(stats.get('sl_count', 0))
                        daily_stats['timeout_count'] = int(stats.get('timeout_count', 0))
                        daily_stats['daily_pnl'] = float(stats.get('total_pnl', 0))
                        
                        # Для trailing stop стратегии учитываем trailing_wins и trailing_losses
                        if use_trailing_stop:
                            trailing_wins = int(stats.get('trailing_wins', 0))
                            trailing_losses = int(stats.get('trailing_losses', 0))
                            daily_stats['trailing_wins'] = trailing_wins
                            daily_stats['trailing_losses'] = trailing_losses
                            total_wins = daily_stats['tp_count'] + trailing_wins
                            total_losses = daily_stats['sl_count'] + trailing_losses
                        else:
                            total_wins = daily_stats['tp_count']
                            total_losses = daily_stats['sl_count']
                        
                        # Обновляем общую статистику
                        combination_result['total_signals'] += daily_stats['signal_count']
                        combination_result['total_pnl'] += daily_stats['daily_pnl']
                        combination_result['total_wins'] += total_wins
                        combination_result['total_losses'] += total_losses
                        
                        # Очищаем временные данные из БД
                        cleanup_query = """
                            DELETE FROM web.scoring_analysis_temp
                            WHERE session_id = %s AND user_id = %s
                        """
                        db.execute_query(cleanup_query, (session_id, current_user.id))
                    
                    combination_result['daily_breakdown'].append(daily_stats)
                    current_date += timedelta(days=1)
                
                # Рассчитываем win rate
                if combination_result['total_signals'] > 0:
                    total_closed = combination_result['total_wins'] + combination_result['total_losses']
                    if total_closed > 0:
                        combination_result['win_rate'] = (combination_result['total_wins'] / total_closed) * 100
                    
                    # Добавляем результат только если были сигналы
                    results.append(combination_result)
        
        # Сортируем результаты по убыванию P&L
        results.sort(key=lambda x: x['total_pnl'], reverse=True)
        
        return jsonify({
            'status': 'success',
            'data': results
        })
        
    except Exception as e:
        logger.error(f"Ошибка анализа эффективности за 30 дней: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/efficiency/analyze', methods=['POST'])
@login_required
def api_efficiency_analyze():
    """API для анализа эффективности с различными параметрами скоринга"""
    try:
        data = request.get_json()
        analysis_type = data.get('type', 'score_total')  # score_total, indicator, pattern, combination
        
        results = []
        
        if analysis_type == 'score_total':
            # Анализ по total_score от 60 до 99
            for score_min in range(60, 100):
                pnl_data = _calculate_efficiency_pnl(
                    score_type='total_score',
                    score_min=score_min
                )
                if pnl_data['signal_count'] > 0:  # Только если есть сигналы
                    results.append({
                        'score_min': score_min,
                        'total_pnl': pnl_data['total_pnl'],
                        'signal_count': pnl_data['signal_count'],
                        'win_rate': pnl_data['win_rate'],
                        'daily_data': pnl_data['daily_data']
                    })
                
        elif analysis_type == 'indicator':
            # Анализ по indicator_score от 60 до 99
            for score_min in range(60, 100):
                pnl_data = _calculate_efficiency_pnl(
                    score_type='indicator_score',
                    score_min=score_min
                )
                if pnl_data['signal_count'] > 0:
                    results.append({
                        'score_min': score_min,
                        'total_pnl': pnl_data['total_pnl'],
                        'signal_count': pnl_data['signal_count'],
                        'win_rate': pnl_data['win_rate'],
                        'daily_data': pnl_data['daily_data']
                    })
                
        elif analysis_type == 'pattern':
            # Анализ по pattern_score от 60 до 99
            for score_min in range(60, 100):
                pnl_data = _calculate_efficiency_pnl(
                    score_type='pattern_score',
                    score_min=score_min
                )
                if pnl_data['signal_count'] > 0:
                    results.append({
                        'score_min': score_min,
                        'total_pnl': pnl_data['total_pnl'],
                        'signal_count': pnl_data['signal_count'],
                        'win_rate': pnl_data['win_rate'],
                        'daily_data': pnl_data['daily_data']
                    })
                    
        elif analysis_type == 'combined':
            # Анализ по комбинациям total_score и indicator_score
            for total_min in range(60, 100, 5):  # С шагом 5 для ускорения
                for indicator_min in range(60, 100, 5):
                    pnl_data = _calculate_efficiency_pnl_combined(
                        total_score_min=total_min,
                        indicator_score_min=indicator_min
                    )
                    if pnl_data['signal_count'] > 0:
                        results.append({
                            'total_score_min': total_min,
                            'indicator_score_min': indicator_min,
                            'total_pnl': pnl_data['total_pnl'],
                            'signal_count': pnl_data['signal_count'],
                            'win_rate': pnl_data['win_rate'],
                            'daily_data': pnl_data['daily_data']
                        })
        
        # Сортируем по убыванию total_pnl
        results.sort(key=lambda x: x['total_pnl'], reverse=True)
        
        return jsonify({
            'status': 'success',
            'results': results[:50]  # Возвращаем топ-50 результатов
        })
        
    except Exception as e:
        logger.error(f"Ошибка анализа эффективности: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


def _calculate_efficiency_pnl(score_type='total_score', score_min=60):
    """Вспомогательная функция для расчета P&L за 30 дней по одному типу скоринга"""
    try:
        # Правильный запрос с учетом реальной структуры таблиц
        query = f"""
            WITH filtered_results AS (
                SELECT 
                    DATE(shr.signal_timestamp) as signal_date,
                    shr.pnl_usd,
                    shr.is_win,
                    shr.close_reason,
                    shr.signal_type
                FROM web.scoring_history_results_v2 shr
                WHERE shr.scoring_history_id IN (
                    SELECT id FROM fas_v2.scoring_history
                    WHERE {score_type} >= %s
                )
                AND shr.signal_timestamp >= NOW() - INTERVAL '32 days'
                AND shr.signal_timestamp < NOW() - INTERVAL '2 days'
                AND shr.is_closed = true
            )
            SELECT 
                signal_date,
                COUNT(*) as signal_count,
                SUM(pnl_usd) as daily_pnl,
                SUM(CASE WHEN is_win = true THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN is_win = false THEN 1 ELSE 0 END) as losses
            FROM filtered_results
            GROUP BY signal_date
            ORDER BY signal_date DESC
        """
        
        results = db.execute_query(query, (score_min,), fetch=True)
        
        daily_data = []
        total_pnl = 0
        total_signals = 0
        total_wins = 0
        total_losses = 0
        
        for row in results:
            daily_pnl = float(row['daily_pnl'] or 0)
            total_pnl += daily_pnl
            total_signals += row['signal_count']
            total_wins += row['wins'] or 0
            total_losses += row['losses'] or 0
            
            daily_data.append({
                'date': row['signal_date'].strftime('%Y-%m-%d'),
                'signal_count': row['signal_count'],
                'daily_pnl': round(daily_pnl, 2),
                'wins': row['wins'],
                'losses': row['losses']
            })
        
        win_rate = 0
        if total_wins + total_losses > 0:
            win_rate = round(total_wins / (total_wins + total_losses) * 100, 1)
        
        return {
            'total_pnl': round(total_pnl, 2),
            'signal_count': total_signals,
            'win_rate': win_rate,
            'daily_data': daily_data
        }
        
    except Exception as e:
        logger.error(f"Ошибка расчета P&L: {e}")
        return {
            'total_pnl': 0,
            'signal_count': 0,
            'win_rate': 0,
            'daily_data': []
        }


def _calculate_efficiency_pnl_combined(total_score_min=60, indicator_score_min=60):
    """Вспомогательная функция для расчета P&L с комбинированными фильтрами"""
    try:
        query = """
            WITH filtered_results AS (
                SELECT 
                    DATE(shr.signal_timestamp) as signal_date,
                    shr.pnl_usd,
                    shr.is_win,
                    shr.close_reason,
                    shr.signal_type
                FROM web.scoring_history_results_v2 shr
                WHERE shr.scoring_history_id IN (
                    SELECT id FROM fas_v2.scoring_history
                    WHERE total_score >= %s AND indicator_score >= %s
                )
                AND shr.signal_timestamp >= NOW() - INTERVAL '32 days'
                AND shr.signal_timestamp < NOW() - INTERVAL '2 days'
                AND shr.is_closed = true
            )
            SELECT 
                signal_date,
                COUNT(*) as signal_count,
                SUM(pnl_usd) as daily_pnl,
                SUM(CASE WHEN is_win = true THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN is_win = false THEN 1 ELSE 0 END) as losses
            FROM filtered_results
            GROUP BY signal_date
            ORDER BY signal_date DESC
        """
        
        results = db.execute_query(query, (total_score_min, indicator_score_min), fetch=True)
        
        daily_data = []
        total_pnl = 0
        total_signals = 0
        total_wins = 0
        total_losses = 0
        
        for row in results:
            daily_pnl = float(row['daily_pnl'] or 0)
            total_pnl += daily_pnl
            total_signals += row['signal_count']
            total_wins += row['wins'] or 0
            total_losses += row['losses'] or 0
            
            daily_data.append({
                'date': row['signal_date'].strftime('%Y-%m-%d'),
                'signal_count': row['signal_count'],
                'daily_pnl': round(daily_pnl, 2),
                'wins': row['wins'],
                'losses': row['losses']
            })
        
        win_rate = 0
        if total_wins + total_losses > 0:
            win_rate = round(total_wins / (total_wins + total_losses) * 100, 1)
        
        return {
            'total_pnl': round(total_pnl, 2),
            'signal_count': total_signals,
            'win_rate': win_rate,
            'daily_data': daily_data
        }
        
    except Exception as e:
        logger.error(f"Ошибка расчета комбинированного P&L: {e}")
        return {
            'total_pnl': 0,
            'signal_count': 0,
            'win_rate': 0,
            'daily_data': []
        }

# API endpoints для получения сохраненных результатов анализа
@app.route('/api/analysis/get_cached_results/<analysis_type>')
@login_required
def get_cached_analysis_results(analysis_type):
    """Получение сохраненных результатов анализа"""
    try:
        user_id = current_user.id
        
        # Проверяем тип анализа
        if analysis_type not in ['efficiency', 'tp_sl', 'trailing']:
            return jsonify({
                'status': 'error',
                'message': 'Неверный тип анализа'
            }), 400
        
        # Проверяем наличие сохраненных результатов
        if user_id not in analysis_results_cache[analysis_type]:
            return jsonify({
                'status': 'success',
                'has_cached': False,
                'data': None
            })
        
        # Возвращаем сохраненные результаты
        cached_data = analysis_results_cache[analysis_type][user_id]
        return jsonify({
            'status': 'success',
            'has_cached': True,
            'data': cached_data
        })
        
    except Exception as e:
        logger.error(f"Ошибка при получении кэшированных результатов: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@app.route('/api/analysis/clear_cached_results/<analysis_type>', methods=['POST'])
@login_required
def clear_cached_analysis_results(analysis_type):
    """Очистка сохраненных результатов анализа"""
    try:
        user_id = current_user.id
        
        # Проверяем тип анализа
        if analysis_type not in ['efficiency', 'tp_sl', 'trailing', 'all']:
            return jsonify({
                'status': 'error',
                'message': 'Неверный тип анализа'
            }), 400
        
        # Очищаем результаты в памяти
        if analysis_type == 'all':
            for cache_type in ['efficiency', 'tp_sl', 'trailing']:
                if user_id in analysis_results_cache[cache_type]:
                    del analysis_results_cache[cache_type][user_id]
        else:
            if user_id in analysis_results_cache[analysis_type]:
                del analysis_results_cache[analysis_type][user_id]
        
        # Очищаем кэш в БД
        clear_db_cache_query = """
            DELETE FROM web.efficiency_cache 
            WHERE user_id = %s
        """
        db.execute_query(clear_db_cache_query, (user_id,))
        
        return jsonify({
            'status': 'success',
            'message': 'Результаты и кэш успешно очищены'
        })
        
    except Exception as e:
        logger.error(f"Ошибка при очистке кэшированных результатов: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

# Обработка ошибок
@app.errorhandler(404)
def not_found(error):
    """Обработка ошибки 404"""
    return render_template('404.html'), 404

@app.errorhandler(500)
def internal_error(error):
    """Обработка ошибки 500"""
    logger.error(f"Внутренняя ошибка сервера: {error}")
    return render_template('500.html'), 500

@app.route('/api/reinitialize_signals', methods=['POST'])
@login_required
def api_reinitialize_signals():
    """API для переинициализации сигналов с новыми параметрами"""
    try:
        data = request.get_json()
        take_profit = float(data.get('take_profit', 4.0))
        stop_loss = float(data.get('stop_loss', 3.0))
        hours = int(data.get('hours', 48))

        # Получаем настройки пользователя
        filters_query = """
            SELECT * FROM web.user_signal_filters
            WHERE user_id = %s
        """
        user_filters = db.execute_query(filters_query, (current_user.id,), fetch=True)

        if user_filters:
            filters = user_filters[0]
        else:
            # Если записи нет, создаем её со значениями по умолчанию
            # Используем дефолтные значения из Config
            default_filters = Config.get_default_user_filters()
            
            # Создаем запись в БД для пользователя
            insert_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, hide_younger_than_hours, hide_older_than_hours,
                    stop_loss_percent, take_profit_percent, position_size_usd,
                    leverage, use_trailing_stop, trailing_distance_pct,
                    trailing_activation_pct, score_week_min, score_month_min,
                    allowed_hours, max_trades_per_15min
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """
            db.execute_query(insert_query, (
                current_user.id,
                default_filters['hide_younger_than_hours'],
                default_filters['hide_older_than_hours'],
                default_filters['stop_loss_percent'],
                default_filters['take_profit_percent'],
                default_filters['position_size_usd'],
                default_filters['leverage'],
                default_filters['use_trailing_stop'],
                default_filters['trailing_distance_pct'],
                default_filters['trailing_activation_pct'],
                default_filters['score_week_min'],
                default_filters['score_month_min'],
                default_filters['allowed_hours'],
                default_filters.get('max_trades_per_15min', 3)
            ))
            
            filters = default_filters

        # Обновляем настройки пользователя
        update_query = """
            INSERT INTO web.user_signal_filters
            (user_id, take_profit_percent, stop_loss_percent, updated_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (user_id)
            DO UPDATE SET
                take_profit_percent = EXCLUDED.take_profit_percent,
                stop_loss_percent = EXCLUDED.stop_loss_percent,
                updated_at = NOW()
        """
        db.execute_query(update_query, (current_user.id, take_profit, stop_loss))

        # Логируем действие
        print(f"[REINITIALIZE] Пользователь {current_user.id} обновил настройки: TP={take_profit}%, SL={stop_loss}%")

        return jsonify({
            'status': 'success',
            'message': f'Настройки обновлены: TP={take_profit}%, SL={stop_loss}%',
            'take_profit': take_profit,
            'stop_loss': stop_loss
        })

    except Exception as e:
        logger.error(f"Ошибка переинициализации сигналов: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


# ============================================================================
# BACKTEST STRATEGY ANALYSIS - Новый раздел для анализа бэктеста
# ============================================================================

@app.route('/backtest_strategy')
@login_required
def backtest_strategy():
    """Страница анализа backtest стратегии"""
    return render_template('backtest_strategy.html')


@app.route('/backtest_strategy_binance')
@login_required
def backtest_strategy_binance():
    """Страница анализа backtest стратегии - Binance only"""
    return render_template('backtest_strategy_binance.html')


@app.route('/backtest_strategy_bybit')
@login_required
def backtest_strategy_bybit():
    """Страница анализа backtest стратегии - Bybit only"""
    return render_template('backtest_strategy_bybit.html')


@app.route('/api/backtest/latest_session', methods=['GET'])
@login_required
def api_backtest_latest_session():
    """API: Получить последнюю сессию бэктеста с топ-50 комбинациями"""
    try:
        sort_by = request.args.get('sort_by', 'final_equity')  # 'final_equity' or 'win_rate'
        limit = int(request.args.get('limit', 50))

        # Получаем последнюю сессию
        session_query = """
            SELECT session_id, start_time, end_time, status, parameters, description
            FROM web.backtest_sessions
            ORDER BY start_time DESC
            LIMIT 1
        """
        session_result = db.execute_query(session_query, fetch=True)

        if not session_result:
            return jsonify({
                'status': 'error',
                'message': 'Нет доступных сессий бэктеста'
            }), 404

        session = session_result[0]
        session_id = session['session_id']

        # Определяем сортировку
        sort_column = 'final_equity DESC' if sort_by == 'final_equity' else 'win_rate DESC'

        # Получаем топ комбинаций
        summary_query = f"""
            SELECT
                score_week_filter, score_month_filter, max_trades_filter,
                stop_loss_filter, trailing_activation_filter, trailing_distance_filter,
                win_rate, total_pnl_usd, final_equity, total_trades,
                win_count, loss_count, breakeven_count,
                max_concurrent_positions, min_equity, total_commission_usd
            FROM web.backtest_summary
            WHERE session_id = %s
            ORDER BY {sort_column}, total_pnl_usd DESC
            LIMIT %s
        """
        combinations = db.execute_query(summary_query, (session_id, limit), fetch=True)

        return jsonify({
            'status': 'success',
            'session': {
                'session_id': session_id,
                'start_time': session['start_time'].isoformat() if session['start_time'] else None,
                'end_time': session['end_time'].isoformat() if session['end_time'] else None,
                'status': session['status'],
                'description': session['description'],
                'parameters': session['parameters']
            },
            'combinations': [{
                'score_week': c['score_week_filter'],
                'score_month': c['score_month_filter'],
                'max_trades': c['max_trades_filter'],
                'stop_loss': float(c['stop_loss_filter']),
                'trailing_activation': float(c['trailing_activation_filter']),
                'trailing_distance': float(c['trailing_distance_filter']),
                'win_rate': float(c['win_rate'] or 0),
                'total_pnl': float(c['total_pnl_usd'] or 0),
                'final_equity': float(c['final_equity'] or 0),
                'total_trades': c['total_trades'],
                'win_count': c['win_count'],
                'loss_count': c['loss_count'],
                'breakeven_count': c['breakeven_count'],
                'max_concurrent': c['max_concurrent_positions'],
                'min_equity': float(c['min_equity'] or 0),
                'total_commission': float(c['total_commission_usd'] or 0)
            } for c in combinations]
        })

    except Exception as e:
        logger.error(f"Ошибка получения данных бэктеста: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/backtest_binance/latest_session', methods=['GET'])
@login_required
def api_backtest_binance_latest_session():
    """API: Get latest backtest session for Binance"""
    try:
        sort_by = request.args.get('sort_by', 'final_equity')
        limit = int(request.args.get('limit', 50))

        # Validate sort_by
        if sort_by not in ['final_equity', 'win_rate']:
            sort_by = 'final_equity'

        # Get latest session from binance table
        session_query = """
            SELECT session_id, start_time, end_time, status, parameters, description
            FROM web.backtest_sessions_binance
            ORDER BY start_time DESC
            LIMIT 1
        """

        session_result = db.execute_query(session_query, fetch=True)

        if not session_result:
            return jsonify({'status': 'success', 'session': None, 'combinations': []})

        session = session_result[0]
        session_id = session['session_id']

        # Get top combinations
        sort_column = 'final_equity DESC' if sort_by == 'final_equity' else 'win_rate DESC'

        combos_query = f"""
            SELECT
                score_week_filter as score_week,
                score_month_filter as score_month,
                max_trades_filter as max_trades,
                stop_loss_filter as stop_loss,
                trailing_activation_filter as trailing_activation,
                trailing_distance_filter as trailing_distance,
                win_rate,
                total_pnl_usd as total_pnl,
                final_equity,
                total_trades,
                win_count,
                loss_count,
                breakeven_count,
                max_concurrent_positions as max_concurrent,
                min_equity,
                total_commission_usd as total_commission
            FROM web.backtest_summary_binance
            WHERE session_id = %s
            ORDER BY {sort_column}, total_pnl_usd DESC
            LIMIT %s
        """

        combos_result = db.execute_query(combos_query, (session_id, limit), fetch=True)

        # Format response
        session_data = {
            'session_id': str(session['session_id']),
            'start_time': session['start_time'].isoformat() if session['start_time'] else None,
            'end_time': session['end_time'].isoformat() if session['end_time'] else None,
            'status': session['status'],
            'description': session['description'],
            'parameters': session['parameters']
        }

        combinations = []
        if combos_result:
            for row in combos_result:
                combo_dict = {}
                for key, value in dict(row).items():
                    # Convert Decimal to float for JSON serialization
                    if hasattr(value, 'is_finite'):  # Check if it's a Decimal
                        combo_dict[key] = float(value)
                    else:
                        combo_dict[key] = value
                combinations.append(combo_dict)

        return jsonify({
            'status': 'success',
            'session': session_data,
            'combinations': combinations
        })

    except Exception as e:
        logger.error(f"Error in api_backtest_binance_latest_session: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/backtest_bybit/latest_session', methods=['GET'])
@login_required
def api_backtest_bybit_latest_session():
    """API: Get latest backtest session for Bybit"""
    try:
        sort_by = request.args.get('sort_by', 'final_equity')
        limit = int(request.args.get('limit', 50))

        # Validate sort_by
        if sort_by not in ['final_equity', 'win_rate']:
            sort_by = 'final_equity'

        # Get latest session from bybit table
        session_query = """
            SELECT session_id, start_time, end_time, status, parameters, description
            FROM web.backtest_sessions_bybit
            ORDER BY start_time DESC
            LIMIT 1
        """

        session_result = db.execute_query(session_query, fetch=True)

        if not session_result:
            return jsonify({'status': 'success', 'session': None, 'combinations': []})

        session = session_result[0]
        session_id = session['session_id']

        # Get top combinations
        sort_column = 'final_equity DESC' if sort_by == 'final_equity' else 'win_rate DESC'

        combos_query = f"""
            SELECT
                score_week_filter as score_week,
                score_month_filter as score_month,
                max_trades_filter as max_trades,
                stop_loss_filter as stop_loss,
                trailing_activation_filter as trailing_activation,
                trailing_distance_filter as trailing_distance,
                win_rate,
                total_pnl_usd as total_pnl,
                final_equity,
                total_trades,
                win_count,
                loss_count,
                breakeven_count,
                max_concurrent_positions as max_concurrent,
                min_equity,
                total_commission_usd as total_commission
            FROM web.backtest_summary_bybit
            WHERE session_id = %s
            ORDER BY {sort_column}, total_pnl_usd DESC
            LIMIT %s
        """

        combos_result = db.execute_query(combos_query, (session_id, limit), fetch=True)

        # Format response
        session_data = {
            'session_id': str(session['session_id']),
            'start_time': session['start_time'].isoformat() if session['start_time'] else None,
            'end_time': session['end_time'].isoformat() if session['end_time'] else None,
            'status': session['status'],
            'description': session['description'],
            'parameters': session['parameters']
        }

        combinations = []
        if combos_result:
            for row in combos_result:
                combo_dict = {}
                for key, value in dict(row).items():
                    # Convert Decimal to float for JSON serialization
                    if hasattr(value, 'is_finite'):  # Check if it's a Decimal
                        combo_dict[key] = float(value)
                    else:
                        combo_dict[key] = value
                combinations.append(combo_dict)

        return jsonify({
            'status': 'success',
            'session': session_data,
            'combinations': combinations
        })

    except Exception as e:
        logger.error(f"Error in api_backtest_bybit_latest_session: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/backtest/combo_details', methods=['POST'])
@login_required
def api_backtest_combo_details():
    """API: Получить детальную информацию по комбинации (trades по дням)"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        score_week = data.get('score_week')
        score_month = data.get('score_month')
        max_trades = data.get('max_trades')
        stop_loss = data.get('stop_loss')
        trailing_activation = data.get('trailing_activation')
        trailing_distance = data.get('trailing_distance')

        # Получаем агрегированные данные по дням
        query = """
            SELECT
                DATE(signal_timestamp) as day,
                COUNT(*) as total_trades,
                SUM(pnl_usd) as day_pnl,

                -- Liquidation (критический убыток)
                COUNT(CASE WHEN close_reason = 'liquidation' THEN 1 END) as liquidation_count,
                SUM(CASE WHEN close_reason = 'liquidation' THEN pnl_usd ELSE 0 END) as liquidation_loss,

                -- Stop Loss (защитный механизм фазы 1)
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
                SUM(CASE WHEN close_reason = 'stop_loss' THEN pnl_usd ELSE 0 END) as sl_loss,

                -- Trailing Stop (WIN механизм)
                COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as ts_count,
                SUM(CASE WHEN close_reason = 'trailing_stop' THEN pnl_usd ELSE 0 END) as ts_profit,

                -- Breakeven (нейтральное закрытие фазы 2)
                COUNT(CASE WHEN close_reason = 'breakeven' THEN 1 END) as breakeven_count,
                SUM(CASE WHEN close_reason = 'breakeven' THEN pnl_usd ELSE 0 END) as breakeven_pnl,

                -- Smart Loss (прогрессивный убыток фазы 3)
                COUNT(CASE WHEN close_reason = 'smart_loss' THEN 1 END) as smart_loss_count,
                SUM(CASE WHEN close_reason = 'smart_loss' THEN pnl_usd ELSE 0 END) as smart_loss_amount
            FROM web.backtest_results
            WHERE session_id = %s
                AND score_week_filter = %s
                AND score_month_filter = %s
                AND max_trades_filter = %s
                AND stop_loss_filter = %s
                AND trailing_activation_filter = %s
                AND trailing_distance_filter = %s
            GROUP BY DATE(signal_timestamp)
            ORDER BY day
        """

        daily_stats = db.execute_query(query, (
            session_id, score_week, score_month, max_trades,
            stop_loss, trailing_activation, trailing_distance
        ), fetch=True)

        # Вычисляем накопительный баланс
        initial_capital = 1000.0  # Из CONFIG
        balance = initial_capital
        result_data = []

        for stat in daily_stats:
            day_pnl = float(stat['day_pnl'] or 0)
            balance_start = balance
            balance += day_pnl

            result_data.append({
                'day': stat['day'].isoformat(),
                'balance_start': round(balance_start, 2),
                'balance_end': round(balance, 2),
                'day_pnl': round(day_pnl, 2),
                'total_trades': stat['total_trades'],

                # Exit reasons
                'liquidation_count': stat['liquidation_count'] or 0,
                'liquidation_loss': round(float(stat['liquidation_loss'] or 0), 2),

                'sl_count': stat['sl_count'] or 0,
                'sl_loss': round(float(stat['sl_loss'] or 0), 2),

                'ts_count': stat['ts_count'] or 0,
                'ts_profit': round(float(stat['ts_profit'] or 0), 2),

                'breakeven_count': stat['breakeven_count'] or 0,
                'breakeven_pnl': round(float(stat['breakeven_pnl'] or 0), 2),

                'smart_loss_count': stat['smart_loss_count'] or 0,
                'smart_loss_amount': round(float(stat['smart_loss_amount'] or 0), 2)
            })

        return jsonify({
            'status': 'success',
            'daily_stats': result_data
        })

    except Exception as e:
        logger.error(f"Ошибка получения детальной информации: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@app.route('/api/backtest_binance/combo_details', methods=['POST'])
@login_required
def api_backtest_binance_combo_details():
    """API: Get detailed daily stats for Binance combination"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        score_week = data.get('score_week')
        score_month = data.get('score_month')
        max_trades = data.get('max_trades')
        stop_loss = data.get('stop_loss')
        trailing_activation = data.get('trailing_activation')
        trailing_distance = data.get('trailing_distance')

        # Query daily stats from binance results table
        query = """
            SELECT
                DATE(signal_timestamp) as day,
                COUNT(*) as total_trades,
                SUM(pnl_usd) as day_pnl,

                -- Liquidation (критический убыток)
                COUNT(CASE WHEN close_reason = 'liquidation' THEN 1 END) as liquidation_count,
                SUM(CASE WHEN close_reason = 'liquidation' THEN pnl_usd ELSE 0 END) as liquidation_loss,

                -- Stop Loss (защитный механизм фазы 1)
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
                SUM(CASE WHEN close_reason = 'stop_loss' THEN pnl_usd ELSE 0 END) as sl_loss,

                -- Trailing Stop (WIN механизм)
                COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as ts_count,
                SUM(CASE WHEN close_reason = 'trailing_stop' THEN pnl_usd ELSE 0 END) as ts_profit,

                -- Breakeven (нейтральное закрытие фазы 2)
                COUNT(CASE WHEN close_reason = 'breakeven' THEN 1 END) as breakeven_count,
                SUM(CASE WHEN close_reason = 'breakeven' THEN pnl_usd ELSE 0 END) as breakeven_pnl,

                -- Smart Loss (прогрессивный убыток фазы 3)
                COUNT(CASE WHEN close_reason = 'smart_loss' THEN 1 END) as smart_loss_count,
                SUM(CASE WHEN close_reason = 'smart_loss' THEN pnl_usd ELSE 0 END) as smart_loss_amount
            FROM web.backtest_results_binance
            WHERE session_id = %s
                AND score_week_filter = %s
                AND score_month_filter = %s
                AND max_trades_filter = %s
                AND stop_loss_filter = %s
                AND trailing_activation_filter = %s
                AND trailing_distance_filter = %s
            GROUP BY DATE(signal_timestamp)
            ORDER BY day
        """

        daily_stats = db.execute_query(query, (
            session_id, score_week, score_month, max_trades,
            stop_loss, trailing_activation, trailing_distance
        ), fetch=True)

        # Calculate cumulative balance
        initial_capital = 1000.0
        balance = initial_capital
        result_data = []

        for stat in daily_stats:
            day_pnl = float(stat['day_pnl'] or 0)
            balance_start = balance
            balance += day_pnl

            result_data.append({
                'day': stat['day'].isoformat(),
                'balance_start': round(balance_start, 2),
                'balance_end': round(balance, 2),
                'day_pnl': round(day_pnl, 2),
                'total_trades': stat['total_trades'],

                # Exit reasons
                'liquidation_count': stat['liquidation_count'] or 0,
                'liquidation_loss': round(float(stat['liquidation_loss'] or 0), 2),

                'sl_count': stat['sl_count'] or 0,
                'sl_loss': round(float(stat['sl_loss'] or 0), 2),

                'ts_count': stat['ts_count'] or 0,
                'ts_profit': round(float(stat['ts_profit'] or 0), 2),

                'breakeven_count': stat['breakeven_count'] or 0,
                'breakeven_pnl': round(float(stat['breakeven_pnl'] or 0), 2),

                'smart_loss_count': stat['smart_loss_count'] or 0,
                'smart_loss_amount': round(float(stat['smart_loss_amount'] or 0), 2)
            })

        return jsonify({
            'status': 'success',
            'daily_stats': result_data
        })

    except Exception as e:
        logger.error(f"Error in api_backtest_binance_combo_details: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/backtest_bybit/combo_details', methods=['POST'])
@login_required
def api_backtest_bybit_combo_details():
    """API: Get detailed daily stats for Bybit combination"""
    try:
        data = request.get_json()
        session_id = data.get('session_id')
        score_week = data.get('score_week')
        score_month = data.get('score_month')
        max_trades = data.get('max_trades')
        stop_loss = data.get('stop_loss')
        trailing_activation = data.get('trailing_activation')
        trailing_distance = data.get('trailing_distance')

        # Query daily stats from bybit results table
        query = """
            SELECT
                DATE(signal_timestamp) as day,
                COUNT(*) as total_trades,
                SUM(pnl_usd) as day_pnl,

                -- Liquidation (критический убыток)
                COUNT(CASE WHEN close_reason = 'liquidation' THEN 1 END) as liquidation_count,
                SUM(CASE WHEN close_reason = 'liquidation' THEN pnl_usd ELSE 0 END) as liquidation_loss,

                -- Stop Loss (защитный механизм фазы 1)
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_count,
                SUM(CASE WHEN close_reason = 'stop_loss' THEN pnl_usd ELSE 0 END) as sl_loss,

                -- Trailing Stop (WIN механизм)
                COUNT(CASE WHEN close_reason = 'trailing_stop' THEN 1 END) as ts_count,
                SUM(CASE WHEN close_reason = 'trailing_stop' THEN pnl_usd ELSE 0 END) as ts_profit,

                -- Breakeven (нейтральное закрытие фазы 2)
                COUNT(CASE WHEN close_reason = 'breakeven' THEN 1 END) as breakeven_count,
                SUM(CASE WHEN close_reason = 'breakeven' THEN pnl_usd ELSE 0 END) as breakeven_pnl,

                -- Smart Loss (прогрессивный убыток фазы 3)
                COUNT(CASE WHEN close_reason = 'smart_loss' THEN 1 END) as smart_loss_count,
                SUM(CASE WHEN close_reason = 'smart_loss' THEN pnl_usd ELSE 0 END) as smart_loss_amount
            FROM web.backtest_results_bybit
            WHERE session_id = %s
                AND score_week_filter = %s
                AND score_month_filter = %s
                AND max_trades_filter = %s
                AND stop_loss_filter = %s
                AND trailing_activation_filter = %s
                AND trailing_distance_filter = %s
            GROUP BY DATE(signal_timestamp)
            ORDER BY day
        """

        daily_stats = db.execute_query(query, (
            session_id, score_week, score_month, max_trades,
            stop_loss, trailing_activation, trailing_distance
        ), fetch=True)

        # Calculate cumulative balance
        initial_capital = 1000.0
        balance = initial_capital
        result_data = []

        for stat in daily_stats:
            day_pnl = float(stat['day_pnl'] or 0)
            balance_start = balance
            balance += day_pnl

            result_data.append({
                'day': stat['day'].isoformat(),
                'balance_start': round(balance_start, 2),
                'balance_end': round(balance, 2),
                'day_pnl': round(day_pnl, 2),
                'total_trades': stat['total_trades'],

                # Exit reasons
                'liquidation_count': stat['liquidation_count'] or 0,
                'liquidation_loss': round(float(stat['liquidation_loss'] or 0), 2),

                'sl_count': stat['sl_count'] or 0,
                'sl_loss': round(float(stat['sl_loss'] or 0), 2),

                'ts_count': stat['ts_count'] or 0,
                'ts_profit': round(float(stat['ts_profit'] or 0), 2),

                'breakeven_count': stat['breakeven_count'] or 0,
                'breakeven_pnl': round(float(stat['breakeven_pnl'] or 0), 2),

                'smart_loss_count': stat['smart_loss_count'] or 0,
                'smart_loss_amount': round(float(stat['smart_loss_amount'] or 0), 2)
            })

        return jsonify({
            'status': 'success',
            'daily_stats': result_data
        })

    except Exception as e:
        logger.error(f"Error in api_backtest_bybit_combo_details: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# ============================================================================
# WHALE FUTURES - Restored from nginx logs
# ============================================================================

@app.route('/whale_futures')
@login_required
def whale_futures():
    """Whale Futures tracking page"""
    return render_template('whale_futures.html')


@app.route('/api/whale_futures/stats', methods=['GET'])
@login_required
def api_whale_futures_stats():
    """API: Get aggregated whale statistics"""
    try:
        time_filter = request.args.get('time_filter', '24h')
        min_size_usd = float(request.args.get('min_size_usd', 500000))

        # Parse time filter
        hours = int(time_filter.replace('h', ''))

        # Query aggregated stats
        stats_query = """
            SELECT
                COUNT(*) as total_positions,
                COUNT(DISTINCT symbol) as unique_symbols,
                SUM(CASE WHEN side = 'LONG' THEN 1 ELSE 0 END) as long_count,
                SUM(CASE WHEN side = 'SHORT' THEN 1 ELSE 0 END) as short_count,
                SUM(CASE WHEN side = 'LONG' THEN size_usd ELSE 0 END) as long_volume_usd,
                SUM(CASE WHEN side = 'SHORT' THEN size_usd ELSE 0 END) as short_volume_usd,
                SUM(size_usd) as total_volume_usd
            FROM web.whale_positions
            WHERE trade_timestamp >= NOW() - INTERVAL '1 hour' * %s
                AND size_usd >= %s
        """

        result = db.execute_query(stats_query, (hours, min_size_usd), fetch=True)

        if result:
            stats = result[0]
            return jsonify({
                'status': 'success',
                'stats': {
                    'total_positions': int(stats['total_positions'] or 0),
                    'unique_symbols': int(stats['unique_symbols'] or 0),
                    'long_count': int(stats['long_count'] or 0),
                    'short_count': int(stats['short_count'] or 0),
                    'long_volume_usd': float(stats['long_volume_usd'] or 0),
                    'short_volume_usd': float(stats['short_volume_usd'] or 0),
                    'total_volume_usd': float(stats['total_volume_usd'] or 0)
                }
            })
        else:
            return jsonify({'status': 'success', 'stats': {}})

    except Exception as e:
        logger.error(f"Error in whale_futures_stats: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/whale_futures/symbols', methods=['GET'])
@login_required
def api_whale_futures_symbols():
    """API: Get aggregated Long/Short by normalized symbols"""
    try:
        time_filter = request.args.get('time_filter', '24h')
        min_size_usd = float(request.args.get('min_size_usd', 500000))

        # Parse time filter
        hours = int(time_filter.replace('h', ''))

        # Normalize symbols: BTC-PERP, BTCUSDT -> BTC
        symbols_query = """
            WITH normalized_symbols AS (
                SELECT
                    CASE
                        WHEN symbol LIKE '%%-PERP' THEN SUBSTRING(symbol FROM 1 FOR POSITION('-' IN symbol) - 1)
                        WHEN symbol LIKE '%%USDT' THEN SUBSTRING(symbol FROM 1 FOR LENGTH(symbol) - 4)
                        WHEN symbol LIKE '%%USDC' THEN SUBSTRING(symbol FROM 1 FOR LENGTH(symbol) - 4)
                        ELSE symbol
                    END as normalized_symbol,
                    side,
                    size_usd
                FROM web.whale_positions
                WHERE trade_timestamp >= NOW() - INTERVAL '1 hour' * %s
                    AND size_usd >= %s
            )
            SELECT
                normalized_symbol,
                SUM(CASE WHEN side = 'LONG' THEN size_usd ELSE 0 END) as long_volume,
                SUM(CASE WHEN side = 'SHORT' THEN size_usd ELSE 0 END) as short_volume,
                COUNT(*) as total_positions
            FROM normalized_symbols
            GROUP BY normalized_symbol
            HAVING SUM(size_usd) > 0
            ORDER BY (SUM(CASE WHEN side = 'LONG' THEN size_usd ELSE 0 END) +
                     SUM(CASE WHEN side = 'SHORT' THEN size_usd ELSE 0 END)) DESC
            LIMIT 20
        """

        results = db.execute_query(symbols_query, (hours, min_size_usd), fetch=True)

        symbols_data = []
        if results:
            for row in results:
                symbols_data.append({
                    'symbol': row['normalized_symbol'],
                    'long_volume': float(row['long_volume']),
                    'short_volume': float(row['short_volume']),
                    'total_positions': int(row['total_positions']),
                    'net_volume': float(row['long_volume']) - float(row['short_volume'])
                })

        return jsonify({
            'status': 'success',
            'data': symbols_data
        })

    except Exception as e:
        import traceback
        error_msg = f"Error in whale_futures_symbols: {e}\n{traceback.format_exc()}"
        logger.error(error_msg)
        print(error_msg, flush=True)  # Force output to stderr
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/api/whale_futures/data', methods=['GET'])
@login_required
def api_whale_futures_data():
    """API: Get whale positions data"""
    try:
        time_filter = request.args.get('time_filter', '24h')
        min_size_usd = float(request.args.get('min_size_usd', 500000))
        side = request.args.get('side', 'both')
        exchange = request.args.get('exchange', 'all')
        limit = int(request.args.get('limit', 100))

        # Parse time filter
        hours = int(time_filter.replace('h', ''))

        # Build query
        query = """
            SELECT
                id,
                exchange,
                symbol,
                side,
                size_usd,
                price,
                trade_timestamp,
                wallet_address,
                trade_count,
                duration_sec,
                source,
                net_position_usd,
                is_market_maker,
                created_at
            FROM web.whale_positions
            WHERE trade_timestamp >= NOW() - INTERVAL '1 hour' * %s
                AND size_usd >= %s
        """
        params = [hours, min_size_usd]

        # Add side filter
        if side != 'both':
            query += " AND side = %s"
            params.append(side.upper())

        # Add exchange filter
        if exchange != 'all':
            query += " AND exchange = %s"
            params.append(exchange)

        query += " ORDER BY trade_timestamp DESC LIMIT %s"
        params.append(limit)

        results = db.execute_query(query, tuple(params), fetch=True)

        positions = []
        if results:
            for row in results:
                positions.append({
                    'id': row['id'],
                    'exchange': row['exchange'],
                    'symbol': row['symbol'],
                    'side': row['side'],
                    'size_usd': float(row['size_usd']),
                    'price': float(row['price']) if row['price'] else None,
                    'trade_timestamp': row['trade_timestamp'].isoformat() if row['trade_timestamp'] else None,
                    'wallet_address': row['wallet_address'],
                    'trade_count': int(row['trade_count']) if row['trade_count'] else None,
                    'duration_sec': float(row['duration_sec']) if row['duration_sec'] else None,
                    'source': row['source'],
                    'net_position_usd': float(row['net_position_usd']) if row['net_position_usd'] else None,
                    'is_market_maker': row['is_market_maker']
                })

        return jsonify({
            'status': 'success',
            'data': positions,
            'count': len(positions)
        })

    except Exception as e:
        logger.error(f"Error in whale_futures_data: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


# =============================================================================
# RAW SIGNALS FEATURE - Endpoints for displaying signals from fas_v2.scoring_history
# =============================================================================

@app.route('/raw_signals')
@login_required
def raw_signals():
    """Страница отображения сырых сигналов"""
    return render_template('raw_signals.html')


@app.route('/api/raw_signals/list', methods=['POST'])
@login_required
def api_get_raw_signals():
    """
    API для получения списка сырых сигналов с фильтрацией и пагинацией

    POST Body:
    {
        "filters": {
            "time_range": "24h",
            "score_week_min": -100,
            "score_week_max": 100,
            "score_month_min": -100,
            "score_month_max": 100,
            "actions": ["BUY", "SELL", "LONG", "SHORT", "NEUTRAL", "NO_TRADE"],
            "patterns": ["OI_EXPLOSION", ...],
            "regimes": ["BULL", "BEAR", "NEUTRAL"],
            "exchanges": [1, 2]
        },
        "page": 1,
        "per_page": 50
    }

    Returns:
    {
        "signals": [...],
        "total": int,
        "page": int,
        "pages": int
    }
    """
    try:
        # DEBUG
        with open('/tmp/api_debug.log', 'a') as f:
            import datetime
            f.write(f"\n{datetime.datetime.now()}: API endpoint called\n")
            f.flush()

        data = request.get_json()
        filters = data.get('filters', {})
        page = data.get('page', 1)
        per_page = data.get('per_page', 50)

        logger.info(f"API received filters: {filters}, page: {page}, per_page: {per_page}")

        # DEBUG
        with open('/tmp/api_debug.log', 'a') as f:
            f.write(f"  Filters: {filters}\n")
            f.write(f"  About to call get_raw_signals...\n")
            f.flush()

        # Валидация параметров
        page = max(1, int(page))
        per_page = max(1, min(int(per_page), 100))  # Максимум 100 на страницу

        # Получаем данные
        result = get_raw_signals(db, filters, page, per_page)

        # DEBUG
        with open('/tmp/api_debug.log', 'a') as f:
            f.write(f"  get_raw_signals returned: total={result['total']}\n")
            f.flush()

        logger.info(f"API result: total={result['total']}, signals_count={len(result['signals'])}")

        # DEBUG
        with open('/tmp/api_debug.log', 'a') as f:
            f.write(f"  Converting {len(result['signals'])} signals...\n")
            f.flush()

        # Конвертируем datetime в строки и Decimal в float для JSON
        from decimal import Decimal
        for i, signal in enumerate(result['signals']):
            try:
                if signal.get('timestamp'):
                    signal['timestamp'] = signal['timestamp'].isoformat()
                # Convert Decimal to float for JSON serialization
                for key, value in signal.items():
                    if isinstance(value, Decimal):
                        signal[key] = float(value)
            except Exception as e:
                with open('/tmp/api_debug.log', 'a') as f:
                    f.write(f"  Error converting signal {i}: {e}\n")
                    f.flush()
                raise

        # DEBUG
        with open('/tmp/api_debug.log', 'a') as f:
            f.write(f"  About to jsonify...\n")
            f.flush()

        result_json = jsonify(result)

        # DEBUG
        with open('/tmp/api_debug.log', 'a') as f:
            f.write(f"  Jsonify done, returning\n")
            f.flush()

        return result_json

    except Exception as e:
        logger.error(f"Error in api_get_raw_signals: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': 'Failed to fetch signals',
            'message': str(e)
        }), 500


@app.route('/api/raw_signals/details/<int:signal_id>')
@login_required
def api_get_signal_details(signal_id):
    """
    API для получения детальной информации о сигнале

    Args:
        signal_id: int - ID сигнала из scoring_history

    Returns:
    {
        "signal": {...},
        "patterns": [...],
        "indicators": {...},
        "poc": {...},
        "regime": {...}
    }
    """
    try:
        details = get_signal_details(db, signal_id)

        if not details:
            return jsonify({
                'error': 'Signal not found',
                'signal_id': signal_id
            }), 404

        # Конвертируем datetime в строки и Decimal в float для JSON
        from decimal import Decimal
        def convert_dates(obj):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return {k: convert_dates(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [convert_dates(item) for item in obj]
            if isinstance(obj, Decimal):
                return float(obj)
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            return obj

        details = convert_dates(details)

        return jsonify(details)

    except Exception as e:
        logger.error(f"Error in api_get_signal_details for ID {signal_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': 'Failed to fetch signal details',
            'message': str(e)
        }), 500


@app.route('/api/raw_signals/stats', methods=['POST'])
@login_required
def api_get_raw_signals_stats():
    """
    API для получения статистики по сырым сигналам

    POST Body:
    {
        "filters": {...}  // Те же фильтры что и в /list
    }

    Returns:
    {
        "total": int,
        "by_action": {"BUY": 123, "SELL": 456, ...},
        "by_regime": {"BULL": 234, "BEAR": 345, ...},
        "avg_score_week": float,
        "avg_score_month": float,
        "last_signal_time": "2025-11-05T12:00:00+00:00",
        "pattern_distribution": {"OI_EXPLOSION": 45, ...}
    }
    """
    try:
        data = request.get_json()
        filters = data.get('filters', {})

        stats = get_raw_signals_stats(db, filters)

        # Конвертируем datetime в строки для JSON
        if stats.get('last_signal_time'):
            stats['last_signal_time'] = stats['last_signal_time'].isoformat()

        return jsonify(stats)

    except Exception as e:
        logger.error(f"Error in api_get_raw_signals_stats: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': 'Failed to fetch statistics',
            'message': str(e)
        }), 500


@app.route('/api/raw_signals/export/<int:signal_id>')
@login_required
def api_export_signal(signal_id):
    """
    API для экспорта сигнала в JSON файл

    Args:
        signal_id: int - ID сигнала из scoring_history

    Returns:
        JSON file download
    """
    try:
        details = get_signal_details(db, signal_id)

        if not details:
            return jsonify({
                'error': 'Signal not found',
                'signal_id': signal_id
            }), 404

        # Конвертируем datetime в строки для JSON
        def convert_dates(obj):
            if obj is None:
                return None
            if isinstance(obj, dict):
                return {k: convert_dates(v) for k, v in obj.items()}
            if isinstance(obj, list):
                return [convert_dates(item) for item in obj]
            if hasattr(obj, 'isoformat'):
                return obj.isoformat()
            # Конвертируем Decimal в float
            if hasattr(obj, '__float__'):
                return float(obj)
            return obj

        details = convert_dates(details)

        # Создаем JSON с отступами для читаемости
        import json
        json_data = json.dumps(details, indent=2, ensure_ascii=False)

        # Создаем response для скачивания
        response = make_response(json_data)
        response.headers['Content-Type'] = 'application/json; charset=utf-8'
        response.headers['Content-Disposition'] = f'attachment; filename=signal_{signal_id}.json'

        return response

    except Exception as e:
        logger.error(f"Error in api_export_signal for ID {signal_id}: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            'error': 'Failed to export signal',
            'message': str(e)
        }), 500


# Запуск приложения
if __name__ == '__main__':
    port = Config.PORT
    debug = Config.FLASK_DEBUG

    logger.info(f"Запуск приложения на порту {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
