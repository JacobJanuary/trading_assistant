"""
Основное приложение Flask - Помощник Трейдера
"""
import os
import json
import requests
from decimal import Decimal
from datetime import datetime, timedelta
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from dotenv import load_dotenv
import logging
from datetime import datetime, timedelta, timezone
# Импорт модулей приложения
from database import Database, initialize_signals_with_params, process_signal_complete
from models import User, TradingData, TradingStats

# Загрузка переменных окружения
load_dotenv()

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создание Flask приложения
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')

# Настройка Flask-Login
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Пожалуйста, войдите в систему для доступа к этой странице.'
login_manager.login_message_category = 'info'

# Инициализация базы данных
# Поддерживаем оба способа: через DATABASE_URL или через отдельные параметры
database_url = os.getenv('DATABASE_URL')

# Попробуем сначала отдельные параметры
db_host = os.getenv('DB_HOST')
db_port = os.getenv('DB_PORT', '5432')  # По умолчанию порт PostgreSQL
db_name = os.getenv('DB_NAME')
db_user = os.getenv('DB_USER')
db_password = os.getenv('DB_PASSWORD')

if db_host and db_name and db_user and db_password:
    # Используем отдельные параметры
    db = Database(
        host=db_host,
        port=db_port,
        database=db_name,
        user=db_user,
        password=db_password
    )
    logger.info("База данных инициализирована с отдельными параметрами")
elif database_url:
    # Используем DATABASE_URL
    db = Database(database_url=database_url)
    logger.info("База данных инициализирована с DATABASE_URL")
else:
    logger.error("Не установлены параметры подключения к базе данных. Установите либо DATABASE_URL, либо DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD")
    exit(1)

# Инициализация схемы при первом запуске
try:
    db.initialize_schema()
    logger.info("Схема базы данных инициализирована")
except Exception as e:
    logger.error(f"Ошибка при инициализации схемы: {e}")

@login_manager.user_loader
def load_user(user_id):
    """Загрузка пользователя для Flask-Login"""
    return User.get_by_id(db, int(user_id))

# Проверка доступа для неподтвержденных пользователей
@app.before_request
def check_user_approval():
    """Проверка подтверждения пользователя перед каждым запросом"""
    # Разрешаем доступ к публичным страницам
    public_endpoints = ['login', 'register', 'unauthorized', 'static']
    
    if request.endpoint in public_endpoints or request.path.startswith('/static/'):
        return
    
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
                login_user(user, remember=request.form.get('remember_me'))
                logger.info(f"Пользователь {username} вошел в систему")
                
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
    if min_value_usd is not None and min_value_usd < 10000:
        min_value_usd = 10000
    
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
            min_value_usd=min_value_usd or 10000,
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
    
    if min_value_usd is not None and min_value_usd < 10000:
        min_value_usd = 10000
    
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
    try:
        # Получаем настройки пользователя
        filters_query = """
            SELECT * FROM web.user_signal_filters 
            WHERE user_id = %s
        """
        user_filters = db.execute_query(filters_query, (current_user.id,), fetch=True)

        if user_filters:
            filters = user_filters[0]
        else:
            filters = {
                'hide_younger_than_hours': 6,
                'hide_older_than_hours': 48,
                'stop_loss_percent': 3.00,
                'take_profit_percent': 4.00,
                'position_size_usd': 100.00,
                'leverage': 5,
                'use_trailing_stop': False,
                'trailing_distance_pct': 2.0,
                'trailing_activation_pct': 1.0
            }

        # Получаем параметры из URL для динамического пересчета
        hide_younger = request.args.get('hide_younger', type=int, default=filters['hide_younger_than_hours'])
        hide_older = request.args.get('hide_older', type=int, default=filters['hide_older_than_hours'])
        display_leverage = request.args.get('leverage', type=int, default=filters['leverage'])
        display_position_size = request.args.get('position_size', type=float,
                                                 default=float(filters['position_size_usd']))

        # ========== НОВЫЙ ЗАПРОС НАПРЯМУЮ ИЗ FAS.SCORING_HISTORY ==========
        # Получаем сигналы с фильтрацией по скорингу
        signals_query = """
            SELECT 
                sh.id as signal_id,
                sh.pair_symbol,
                sh.trading_pair_id,
                sh.recommended_action as signal_action,
                sh.timestamp as signal_timestamp,
                sh.total_score,
                sh.indicator_score,
                sh.pattern_score,
                sh.combination_score,
                tp.exchange_id,
                ex.exchange_name
            FROM fas.scoring_history sh
            JOIN public.trading_pairs tp ON tp.id = sh.trading_pair_id
            JOIN public.exchanges ex ON ex.id = tp.exchange_id
            WHERE sh.score_week > 67 
                AND sh.score_month > 67
                AND sh.timestamp >= NOW() - INTERVAL '48 hours'
                AND tp.contract_type_id = 1
                AND tp.exchange_id IN (1, 2)
            ORDER BY sh.timestamp DESC
        """

        raw_signals = db.execute_query(signals_query, fetch=True)

        print(f"[SIGNAL_PERFORMANCE] Найдено {len(raw_signals) if raw_signals else 0} сигналов из scoring_history")

        # Инициализируем пустые данные
        signals_data = []
        processed_signals = []
        efficiency_metrics = {
            'total_signals': 0,
            'open_positions': 0,
            'closed_tp': 0,
            'closed_sl': 0,
            'closed_trailing': 0,
            'trailing_profitable': 0,
            'trailing_loss': 0,
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

            # Очищаем старые данные web_signals для корректной работы
            db.execute_query("TRUNCATE TABLE web.web_signals")

            processed_count = 0
            error_count = 0

            for signal in raw_signals:
                try:
                    # Подготавливаем данные сигнала
                    signal_data = {
                        'signal_id': signal['signal_id'],
                        'pair_symbol': signal['pair_symbol'],
                        'trading_pair_id': signal['trading_pair_id'],
                        'signal_action': signal['signal_action'],
                        'signal_timestamp': make_aware(signal['signal_timestamp']),
                        'exchange_name': signal.get('exchange_name', 'Unknown')
                    }

                    # Обрабатываем сигнал с учетом настроек пользователя
                    result = process_signal_complete(
                        db,
                        signal_data,
                        tp_percent=float(filters['take_profit_percent']),
                        sl_percent=float(filters['stop_loss_percent']),
                        position_size=display_position_size,
                        leverage=display_leverage,
                        use_trailing_stop=filters.get('use_trailing_stop', False),
                        trailing_distance_pct=float(filters.get('trailing_distance_pct', 2.0)),
                        trailing_activation_pct=float(filters.get('trailing_activation_pct', 1.0))
                    )

                    if result['success']:
                        processed_count += 1

                        # Добавляем обработанный сигнал в список
                        processed_signal = {
                            'signal_id': signal['signal_id'],
                            'pair_symbol': signal['pair_symbol'],
                            'signal_action': signal['signal_action'],
                            'signal_timestamp': signal['signal_timestamp'],
                            'exchange_name': signal.get('exchange_name'),
                            'total_score': float(signal.get('total_score', 0)),
                            'indicator_score': float(signal.get('indicator_score', 0)),
                            'pattern_score': float(signal.get('pattern_score', 0)),
                            'combination_score': float(signal.get('combination_score', 0)),
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

            print(f"[SIGNAL_PERFORMANCE] Обработано: {processed_count}, Ошибок: {error_count}")

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
                    entry_price = float(signal['entry_price'])

                    # Определяем текущую цену
                    if signal['is_closed']:
                        current_price = float(signal['closing_price']) if signal['closing_price'] else None
                    else:
                        current_price = current_prices.get(signal['pair_symbol'],
                                                           float(signal['last_known_price'] or 0))

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
                    max_profit = float(signal['max_potential_profit_usd'] or 0)

                    # Возраст сигнала
                    from datetime import datetime, timezone
                    age_hours = (datetime.now(timezone.utc) - signal['signal_timestamp']).total_seconds() / 3600

                    signal_data = {
                        'signal_id': signal['signal_id'],
                        'pair_symbol': signal['pair_symbol'],
                        'signal_action': signal['signal_action'],
                        'signal_timestamp': signal['signal_timestamp'],
                        'age_hours': round(age_hours, 1),
                        'entry_price': entry_price,
                        'current_price': current_price,
                        'is_closed': signal['is_closed'],
                        'close_reason': signal['close_reason'],
                        'pnl_usd': display_pnl,
                        'pnl_percent': price_change_percent,
                        'max_potential_profit_usd': max_profit
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
                        COUNT(CASE 
                            WHEN close_reason = 'trailing_stop' AND realized_pnl_usd > 0 
                            THEN 1 
                        END) as trailing_profitable,
                        COUNT(CASE 
                            WHEN close_reason = 'trailing_stop' AND realized_pnl_usd <= 0 
                            THEN 1 
                        END) as trailing_loss,
                        COALESCE(SUM(realized_pnl_usd), 0) as total_realized,
                        COALESCE(SUM(unrealized_pnl_usd), 0) as total_unrealized,
                        COALESCE(SUM(max_potential_profit_usd), 0) as total_max_potential
                    FROM web.web_signals
                    WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                        AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
                )
                SELECT * FROM signal_stats
            """

            eff_stats = db.execute_query(efficiency_query, (hide_older, hide_younger), fetch=True)

            if eff_stats:
                stats = eff_stats[0]
                efficiency_metrics['total_signals'] = stats['total_signals']
                efficiency_metrics['open_positions'] = stats['open_positions']
                efficiency_metrics['closed_tp'] = stats['closed_tp']
                efficiency_metrics['closed_sl'] = stats['closed_sl']
                efficiency_metrics['closed_trailing'] = stats['closed_trailing'] or 0
                efficiency_metrics['trailing_profitable'] = stats['trailing_profitable'] or 0
                efficiency_metrics['trailing_loss'] = stats['trailing_loss'] or 0
                efficiency_metrics['net_realized_pnl'] = float(stats['total_realized'] or 0)
                efficiency_metrics['unrealized_pnl'] = float(stats['total_unrealized'] or 0)
                efficiency_metrics['total_pnl'] = efficiency_metrics['net_realized_pnl'] + efficiency_metrics[
                    'unrealized_pnl']
                efficiency_metrics['total_max_potential'] = float(stats['total_max_potential'] or 0)

                # Win rate
                total_closed = efficiency_metrics['closed_tp'] + efficiency_metrics['closed_sl'] + efficiency_metrics[
                    'closed_trailing']
                if total_closed > 0:
                    wins = efficiency_metrics['closed_tp'] + efficiency_metrics['trailing_profitable']
                    efficiency_metrics['win_rate'] = (wins / total_closed) * 100

                # TP efficiency
                if efficiency_metrics['total_max_potential'] > 0:
                    efficiency_metrics['tp_efficiency'] = (efficiency_metrics['net_realized_pnl'] /
                                                           efficiency_metrics['total_max_potential']) * 100

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
        stats = {
            'total': len(signals_data),
            'open': efficiency_metrics['open_positions'],
            'closed_tp': efficiency_metrics['closed_tp'],
            'closed_sl': efficiency_metrics['closed_sl'],
            'total_pnl': efficiency_metrics['total_pnl'],
            'win_rate': efficiency_metrics['win_rate']
        }

        return render_template(
            'signal_performance.html',
            signals=signals_data,
            stats=stats,
            efficiency=efficiency_metrics,
            total_stats=total_stats,
            filters={
                'hide_younger_than_hours': hide_younger,
                'hide_older_than_hours': hide_older,
                'stop_loss_percent': float(filters['stop_loss_percent']),
                'take_profit_percent': float(filters['take_profit_percent']),
                'position_size_usd': display_position_size,
                'leverage': display_leverage,
                'saved_leverage': filters['leverage'],
                'saved_position_size': float(filters['position_size_usd']),
                'use_trailing_stop': filters.get('use_trailing_stop', False),
                'trailing_distance_pct': float(filters.get('trailing_distance_pct', 2.0)),
                'trailing_activation_pct': float(filters.get('trailing_activation_pct', 1.0))
            },
            last_update=datetime.now()
        )

    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы сигналов: {e}")
        import traceback
        logger.error(traceback.format_exc())
        flash('Ошибка при загрузке данных сигналов', 'error')
        return redirect(url_for('dashboard'))


@app.route('/scoring_analysis')
@login_required
def scoring_analysis():
    """Страница анализа скоринга"""
    try:
        # Получаем диапазон дат
        date_range = db.execute_query("""
            SELECT 
                MIN(timestamp)::date as min_date,
                (CURRENT_DATE - INTERVAL '2 days')::date as max_date
            FROM fas.scoring_history
        """, fetch=True)[0]

        # Получаем выбранную дату или используем максимальную
        selected_date = request.args.get('date', str(date_range['max_date']))

        # Получаем фильтры из запроса (JSON формат)
        import json
        buy_filters_json = request.args.get('buy_filters', '[]')
        sell_filters_json = request.args.get('sell_filters', '[]')

        try:
            buy_filters = json.loads(buy_filters_json) if buy_filters_json else []
            sell_filters = json.loads(sell_filters_json) if sell_filters_json else []
        except:
            buy_filters = []
            sell_filters = []

        # Параметры расчета
        tp_percent = request.args.get('tp', type=float, default=4.0)
        sl_percent = request.args.get('sl', type=float, default=3.0)
        position_size = request.args.get('position_size', type=float, default=100.0)
        leverage = request.args.get('leverage', type=int, default=5)

        # Получаем сохраненные фильтры пользователя
        from database import get_user_scoring_filters, get_scoring_signals, process_scoring_signals_batch, \
            get_scoring_analysis_results
        import uuid

        saved_filters = get_user_scoring_filters(db, current_user.id)

        # Инициализация данных по умолчанию
        signals_data = []
        stats = {
            'total': 0,
            'buy_signals': 0,
            'sell_signals': 0,
            'total_pnl': 0,
            'tp_count': 0,
            'sl_count': 0,
            'timeout_count': 0,
            'open_count': 0,
            'max_potential': 0,
            'realized_profit': 0,
            'realized_loss': 0
        }
        metrics = {
            'win_rate': 0,
            'tp_efficiency': 0,
            'net_pnl': 0
        }

        # Если есть активные фильтры, получаем и обрабатываем сигналы
        if buy_filters or sell_filters:
            # Получаем сигналы
            raw_signals = get_scoring_signals(db, selected_date, buy_filters, sell_filters)

            # Обрабатываем сигналы
            if raw_signals:
                # Генерируем ID сессии для этого запроса
                session_id = f"scoring_{current_user.id}_{uuid.uuid4().hex[:8]}"

                # Обрабатываем пакетно и сохраняем в БД
                result = process_scoring_signals_batch(
                    db, raw_signals, session_id, current_user.id,
                    tp_percent=tp_percent,
                    sl_percent=sl_percent,
                    position_size=position_size,
                    leverage=leverage
                )

                # Получаем обработанные результаты из БД
                db_signals = get_scoring_analysis_results(db, session_id, current_user.id)

                # Форматируем для отображения
                for signal in db_signals:
                    signals_data.append({
                        'timestamp': signal['signal_timestamp'],
                        'pair_symbol': signal['pair_symbol'],
                        'signal_action': signal['signal_action'],
                        'market_regime': signal['market_regime'],
                        'total_score': float(signal['total_score'] or 0),
                        'indicator_score': float(signal['indicator_score'] or 0),
                        'pattern_score': float(signal['pattern_score'] or 0),
                        'combination_score': float(signal['combination_score'] or 0),
                        'entry_price': float(signal['entry_price']),
                        'current_price': float(signal['close_price']),
                        'is_closed': signal['is_closed'],
                        'close_reason': signal['close_reason'],
                        'hours_to_close': float(signal['hours_to_close'] or 0),
                        'pnl_usd': float(signal['pnl_usd'] or 0),
                        'pnl_percent': float(signal['pnl_percent'] or 0),
                        'max_potential_profit_usd': float(signal['max_potential_profit_usd'] or 0)
                    })

                # Обновляем статистику
                if result and 'stats' in result:
                    db_stats = result['stats']
                    stats = {
                        'total': db_stats['total'] or 0,
                        'buy_signals': db_stats['buy_signals'] or 0,
                        'sell_signals': db_stats['sell_signals'] or 0,
                        'tp_count': db_stats['tp_count'] or 0,
                        'sl_count': db_stats['sl_count'] or 0,
                        'timeout_count': db_stats['timeout_count'] or 0,
                        'total_pnl': float(db_stats['total_pnl'] or 0),
                        'realized_profit': float(db_stats['tp_profit'] or 0),
                        'realized_loss': float(db_stats['sl_loss'] or 0),
                        'max_potential': float(db_stats['total_max_potential'] or 0)
                    }

                    # Рассчитываем метрики
                    total_closed = stats['tp_count'] + stats['sl_count']
                    if total_closed > 0:
                        metrics['win_rate'] = (stats['tp_count'] / total_closed) * 100

                    if stats['max_potential'] > 0:
                        metrics['tp_efficiency'] = (stats['realized_profit'] / stats['max_potential']) * 100

                    metrics['net_pnl'] = stats['total_pnl']

        return render_template(
            'scoring_analysis.html',
            date_range=date_range,
            selected_date=selected_date,
            buy_filters=buy_filters,
            sell_filters=sell_filters,
            saved_filters=saved_filters,
            signals=signals_data,
            stats=stats,
            metrics=metrics,
            params={
                'tp_percent': tp_percent,
                'sl_percent': sl_percent,
                'position_size': position_size,
                'leverage': leverage
            }
        )

    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы скоринга: {e}")
        import traceback
        logger.error(traceback.format_exc())
        flash('Ошибка при загрузке данных скоринга', 'error')
        return redirect(url_for('dashboard'))


@app.route('/api/scoring/apply_filters', methods=['POST'])
@login_required
def api_scoring_apply_filters():
    """API для применения фильтров скоринга с поддержкой Trailing Stop"""
    try:
        data = request.get_json()

        from database import get_scoring_signals, process_scoring_signals_batch, get_scoring_analysis_results
        import uuid

        # Генерируем ID сессии
        session_id = f"scoring_{current_user.id}_{uuid.uuid4().hex[:8]}"

        # Получаем параметры
        selected_date = data.get('date')
        buy_filters = data.get('buy_filters', [])
        sell_filters = data.get('sell_filters', [])
        tp_percent = data.get('tp_percent', 4.0)
        sl_percent = data.get('sl_percent', 3.0)
        position_size = data.get('position_size', 100.0)
        leverage = data.get('leverage', 5)

        # НОВОЕ: Получаем настройки trailing из user_signal_filters
        settings_query = """
            SELECT use_trailing_stop, trailing_distance_pct, trailing_activation_pct
            FROM web.user_signal_filters
            WHERE user_id = %s
        """
        user_settings = db.execute_query(settings_query, (current_user.id,), fetch=True)

        use_trailing_stop = False
        trailing_distance_pct = 2.0
        trailing_activation_pct = 1.0

        if user_settings:
            settings = user_settings[0]
            use_trailing_stop = settings.get('use_trailing_stop', False)
            trailing_distance_pct = float(settings.get('trailing_distance_pct', 2.0))
            trailing_activation_pct = float(settings.get('trailing_activation_pct', 1.0))

        print(f"[API] Обработка фильтров для даты {selected_date}")
        print(f"[API] Режим: {'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'}")
        print(f"[API] BUY фильтров: {len(buy_filters)}, SELL фильтров: {len(sell_filters)}")

        # Получаем сигналы по фильтрам
        raw_signals = get_scoring_signals(db, selected_date, buy_filters, sell_filters)

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
                    'entry_price': float(signal['entry_price']),
                    'current_price': float(signal['close_price']),
                    'is_closed': signal['is_closed'],
                    'close_reason': signal['close_reason'],
                    'hours_to_close': float(signal['hours_to_close'] or 0),
                    'pnl_usd': float(signal['pnl_usd'] or 0),
                    'pnl_percent': float(signal['pnl_percent'] or 0),
                    'max_potential_profit': float(signal['max_potential_profit_usd'] or 0)
                })

            # Статистика с учетом trailing stops
            stats_data = result['stats']

            # Считаем trailing_stop с прибылью как победу
            tp_count = int(stats_data.get('tp_count') or 0)
            trailing_count = int(stats_data.get('trailing_count') or 0)
            total_wins = tp_count + trailing_count  # Все trailing считаем победами если с прибылью

            stats = {
                'total': int(stats_data.get('total') or 0),
                'buy_signals': int(stats_data.get('buy_signals') or 0),
                'sell_signals': int(stats_data.get('sell_signals') or 0),
                'tp_count': total_wins,  # Включаем trailing с прибылью
                'sl_count': int(stats_data.get('sl_count') or 0),
                'trailing_count': trailing_count,  # Отдельный счетчик для UI
                'timeout_count': int(stats_data.get('timeout_count') or 0),
                'total_pnl': float(stats_data.get('total_pnl') or 0),
                'realized_profit': float(stats_data.get('tp_profit') or 0),
                'realized_loss': float(stats_data.get('sl_loss') or 0),
                'max_potential': float(stats_data.get('total_max_potential') or 0),
                'avg_hours_to_close': float(stats_data.get('avg_hours_to_close') or 0),
                'binance_signals': int(stats_data.get('binance_signals') or 0),
                'bybit_signals': int(stats_data.get('bybit_signals') or 0),
                'mode': 'Trailing Stop' if use_trailing_stop else 'Fixed TP/SL'
            }

            # Расчет метрик
            total_closed = stats['tp_count'] + stats['sl_count']
            win_rate = (stats['tp_count'] / total_closed * 100) if total_closed > 0 else 0

            tp_efficiency = 0
            if stats['max_potential'] > 0:
                tp_efficiency = (stats['realized_profit'] / stats['max_potential']) * 100

            metrics = {
                'win_rate': win_rate,
                'tp_efficiency': tp_efficiency,
                'net_pnl': stats['total_pnl'],
                'mode': stats['mode']
            }

            print(f"[API] Статистика: Total={stats['total']}, TP={stats['tp_count']}, "
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
    """API для сохранения фильтров скоринга"""
    try:
        data = request.get_json()

        from database import save_user_scoring_filters

        filter_name = data.get('name', f'Filter_{datetime.now().strftime("%Y%m%d_%H%M%S")}')
        buy_filters = data.get('buy_filters', [])
        sell_filters = data.get('sell_filters', [])

        save_user_scoring_filters(db, current_user.id, filter_name, buy_filters, sell_filters)

        return jsonify({
            'status': 'success',
            'message': 'Фильтры сохранены'
        })

    except Exception as e:
        logger.error(f"Ошибка сохранения фильтров: {e}")
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

        # Получаем параметры или используем текущие из БД
        tp_percent = data.get('take_profit', 4.0)
        sl_percent = data.get('stop_loss', 3.0)
        hours_back = data.get('hours', 48)

        # Сохраняем новые параметры если они изменились
        if 'take_profit' in data or 'stop_loss' in data:
            update_params_query = """
                INSERT INTO web.user_signal_filters (
                    user_id, stop_loss_percent, take_profit_percent
                ) VALUES (%s, %s, %s)
                ON CONFLICT (user_id) DO UPDATE SET
                    stop_loss_percent = EXCLUDED.stop_loss_percent,
                    take_profit_percent = EXCLUDED.take_profit_percent,
                    updated_at = NOW()
            """
            db.execute_query(update_params_query, (current_user.id, sl_percent, tp_percent))

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
            'message': f"Инициализировано {result['initialized']} сигналов. "
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

        # НЕ сохраняем TP/SL здесь - они меняются только через инициализацию
        upsert_query = """
            INSERT INTO web.user_signal_filters (
                user_id, hide_younger_than_hours, hide_older_than_hours,
                position_size_usd, leverage
            ) VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (user_id) DO UPDATE SET
                hide_younger_than_hours = EXCLUDED.hide_younger_than_hours,
                hide_older_than_hours = EXCLUDED.hide_older_than_hours,
                position_size_usd = EXCLUDED.position_size_usd,
                leverage = EXCLUDED.leverage,
                updated_at = NOW()
        """

        db.execute_query(upsert_query, (
            current_user.id, hide_younger, hide_older, position_size, leverage
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
    """API для получения информации о выбранной дате"""
    try:
        data = request.get_json()
        selected_date = data.get('date')
        buy_filters = data.get('buy_filters', [])
        sell_filters = data.get('sell_filters', [])

        # Получаем режимы рынка для этой даты
        market_query = """
            SELECT DISTINCT 
                DATE_TRUNC('hour', timestamp) as hour,
                regime
            FROM fas.market_regime
            WHERE timestamp::date = %s
                AND timeframe = '4h'
            ORDER BY hour
        """
        market_data = db.execute_query(market_query, (selected_date,), fetch=True)

        # Подсчитываем распределение режимов
        regime_counts = {'BULL': 0, 'NEUTRAL': 0, 'BEAR': 0}
        for row in market_data:
            regime = row['regime']
            if regime in regime_counts:
                regime_counts[regime] += 1

        # Определяем доминирующий режим
        dominant_regime = max(regime_counts, key=regime_counts.get)

        # Если есть фильтры, подсчитываем количество сигналов
        signal_count = 0
        if buy_filters or sell_filters:
            from database import get_scoring_signals
            raw_signals = get_scoring_signals(db, selected_date, buy_filters, sell_filters)
            signal_count = len(raw_signals) if raw_signals else 0

        return jsonify({
            'status': 'success',
            'date': selected_date,
            'market_regimes': regime_counts,
            'dominant_regime': dominant_regime,
            'signal_count': signal_count,
            'market_timeline': [
                {'hour': row['hour'].strftime('%H:%M'), 'regime': row['regime']}
                for row in market_data[:6]  # Первые 6 записей для отображения
            ]
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
                   take_profit_percent
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
                'insurance_sl': float(data['stop_loss_percent'] or 3.0),
                'take_profit_percent': float(data['take_profit_percent'] or 4.0)
            })
        else:
            return jsonify({
                'use_trailing_stop': False,
                'take_profit_percent': 4.0,
                'stop_loss_percent': 3.0
            })

    except Exception as e:
        logger.error(f"Ошибка получения режима торговли: {e}")
        return jsonify({'error': str(e)}), 500

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

# Запуск приложения
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    debug = os.getenv('FLASK_DEBUG', 'False').lower() == 'true'
    
    logger.info(f"Запуск приложения на порту {port}")
    app.run(host='0.0.0.0', port=port, debug=debug)
