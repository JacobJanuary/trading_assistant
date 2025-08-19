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
                'leverage': 5
            }

        # Получаем параметры из URL для динамического пересчета
        hide_younger = request.args.get('hide_younger', type=int, default=filters['hide_younger_than_hours'])
        hide_older = request.args.get('hide_older', type=int, default=filters['hide_older_than_hours'])

        # НОВЫЕ параметры для пересчета (из URL или из сохраненных)
        display_leverage = request.args.get('leverage', type=int, default=filters['leverage'])
        display_position_size = request.args.get('position_size', type=float,
                                                 default=float(filters['position_size_usd']))

        # ========== СТАТИСТИКА ЭФФЕКТИВНОСТИ С ПОЛНЫМ ПЕРЕСЧЕТОМ ==========
        efficiency_query = """
            WITH recalculated_pnl AS (
                SELECT 
                    signal_id,
                    pair_symbol,
                    signal_action,
                    entry_price,
                    closing_price,
                    last_known_price,
                    is_closed,
                    close_reason,
                    signal_timestamp,
                    leverage as original_leverage,
                    max_potential_profit_usd,

                    -- Пересчитываем процент изменения цены
                    CASE 
                        WHEN signal_action IN ('SELL', 'SHORT') THEN
                            CASE 
                                WHEN is_closed THEN ((entry_price - closing_price) / entry_price * 100)
                                ELSE ((entry_price - last_known_price) / entry_price * 100)
                            END
                        ELSE
                            CASE 
                                WHEN is_closed THEN ((closing_price - entry_price) / entry_price * 100)
                                ELSE ((last_known_price - entry_price) / entry_price * 100)
                            END
                    END as price_change_percent

                FROM web.web_signals
                WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                    AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
            )
            SELECT 
                COUNT(*) as total_signals,
                COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_positions,
                COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as closed_tp,
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as closed_sl,

                -- P&L с новыми параметрами
                COALESCE(SUM(
                    CASE WHEN close_reason = 'take_profit' THEN
                        %s * (price_change_percent / 100) * %s  -- position_size * percent * leverage
                    END
                ), 0) as tp_realized_profit,

                COALESCE(SUM(
                    CASE WHEN close_reason = 'stop_loss' THEN
                        ABS(%s * (price_change_percent / 100) * %s)
                    END
                ), 0) as sl_realized_loss,

                COALESCE(SUM(
                    CASE WHEN is_closed = FALSE THEN
                        %s * (price_change_percent / 100) * %s
                    END
                ), 0) as unrealized_pnl,

                -- Максимальный профит тоже пересчитываем
                COALESCE(SUM(
                    (max_potential_profit_usd / original_leverage) * %s
                ), 0) as total_max_potential,

                COALESCE(SUM(
                    CASE WHEN close_reason = 'take_profit' THEN
                        (max_potential_profit_usd / original_leverage) * %s
                    END
                ), 0) as tp_max_potential,

                -- Средние проценты
                AVG(CASE WHEN close_reason = 'take_profit' THEN price_change_percent END) as avg_tp_percent,
                AVG(CASE WHEN close_reason = 'stop_loss' THEN price_change_percent END) as avg_sl_percent

            FROM recalculated_pnl
        """

        # Выполняем запрос с новыми параметрами
        efficiency_stats = db.execute_query(
            efficiency_query,
            (hide_older, hide_younger,  # Временные фильтры
             display_position_size, display_leverage,  # TP profit
             display_position_size, display_leverage,  # SL loss
             display_position_size, display_leverage,  # Unrealized
             display_leverage,  # Max potential total
             display_leverage),  # Max potential TP
            fetch=True
        )[0]

        # Теперь все значения уже пересчитаны с новыми параметрами
        tp_realized_profit = float(efficiency_stats['tp_realized_profit'] or 0)
        sl_realized_loss = float(efficiency_stats['sl_realized_loss'] or 0)
        unrealized_pnl = float(efficiency_stats['unrealized_pnl'] or 0)
        total_max_potential = float(efficiency_stats['total_max_potential'] or 0)
        tp_max_potential = float(efficiency_stats['tp_max_potential'] or 0)

        efficiency_metrics = {
            'total_signals': efficiency_stats['total_signals'],
            'open_positions': efficiency_stats['open_positions'],
            'closed_tp': efficiency_stats['closed_tp'],
            'closed_sl': efficiency_stats['closed_sl'],

            'tp_realized_profit': tp_realized_profit,
            'sl_realized_loss': sl_realized_loss,
            'net_realized_pnl': tp_realized_profit - sl_realized_loss,
            'unrealized_pnl': unrealized_pnl,
            'total_pnl': tp_realized_profit - sl_realized_loss + unrealized_pnl,

            'total_max_potential': total_max_potential,
            'tp_max_potential': tp_max_potential,
            'missed_profit': max(0, tp_max_potential - tp_realized_profit),
            'tp_efficiency': (tp_realized_profit / tp_max_potential * 100) if tp_max_potential > 0 else 0,
            'overall_efficiency': ((
                                               tp_realized_profit - sl_realized_loss) / total_max_potential * 100) if total_max_potential > 0 else 0,

            'win_rate': 0,
            'avg_tp_percent': float(efficiency_stats['avg_tp_percent'] or 0),
            'avg_sl_percent': float(efficiency_stats['avg_sl_percent'] or 0)
        }

        # Win rate
        total_closed = efficiency_stats['closed_tp'] + efficiency_stats['closed_sl']
        if total_closed > 0:
            efficiency_metrics['win_rate'] = (efficiency_stats['closed_tp'] / total_closed) * 100

        # ========== ПОЛУЧЕНИЕ СИГНАЛОВ С ПЕРЕСЧЕТОМ ==========

        # Получаем текущие цены
        prices_query = """
            SELECT DISTINCT ON (tp.pair_symbol)
                tp.pair_symbol,
                md.mark_price
            FROM public.trading_pairs tp
            JOIN public.market_data md ON md.trading_pair_id = tp.id
            WHERE tp.contract_type_id = 1
                AND tp.exchange_id = 1
                AND md.capture_time >= NOW() - INTERVAL '5 minutes'
            ORDER BY tp.pair_symbol, md.capture_time DESC
        """
        price_data = db.execute_query(prices_query, fetch=True)
        current_prices = {p['pair_symbol']: float(p['mark_price']) for p in price_data} if price_data else {}

        # Получаем сигналы
        signals_query = """
            SELECT *
            FROM web.web_signals
            WHERE signal_timestamp >= NOW() - (INTERVAL '1 hour' * %s)
                AND signal_timestamp <= NOW() - (INTERVAL '1 hour' * %s)
            ORDER BY signal_timestamp DESC
        """

        signals = db.execute_query(signals_query, (hide_older, hide_younger), fetch=True)

        # Обрабатываем сигналы с ПОЛНЫМ ПЕРЕСЧЕТОМ
        signals_data = []
        stats = {
            'total': 0,
            'open': 0,
            'closed_tp': 0,
            'closed_sl': 0,
            'total_pnl': 0,
            'win_rate': 0
        }

        if signals:
            for signal in signals:
                entry_price = float(signal['entry_price'])
                original_leverage = signal['leverage']  # Оригинальный leverage из БД

                # Определяем текущую цену
                if signal['is_closed']:
                    current_price = float(signal['closing_price']) if signal['closing_price'] else None
                else:
                    current_price = current_prices.get(signal['pair_symbol'], float(signal['last_known_price'] or 0))

                # Рассчитываем процент изменения
                if current_price and entry_price:
                    if signal['signal_action'] in ['SELL', 'SHORT']:
                        price_change_percent = ((entry_price - current_price) / entry_price) * 100
                    else:
                        price_change_percent = ((current_price - entry_price) / entry_price) * 100
                else:
                    price_change_percent = 0

                # ПОЛНЫЙ ПЕРЕСЧЕТ P&L с новыми параметрами
                display_pnl = display_position_size * (price_change_percent / 100) * display_leverage

                # Максимальный профит - пересчитываем с новым leverage
                if signal['max_potential_profit_usd']:
                    # Убираем оригинальный leverage, применяем новый
                    max_profit_base = float(signal['max_potential_profit_usd']) / original_leverage
                    max_profit = max_profit_base * display_leverage
                else:
                    max_profit = 0

                # Возраст сигнала
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

                # Обновляем статистику
                stats['total'] += 1
                stats['total_pnl'] += display_pnl

                if signal['is_closed']:
                    if signal['close_reason'] == 'take_profit':
                        stats['closed_tp'] += 1
                    elif signal['close_reason'] == 'stop_loss':
                        stats['closed_sl'] += 1
                else:
                    stats['open'] += 1

        # Win rate
        total_closed = stats['closed_tp'] + stats['closed_sl']
        if total_closed > 0:
            stats['win_rate'] = (stats['closed_tp'] / total_closed) * 100

        # Общая статистика без фильтров
        total_stats_query = """
            SELECT 
                COUNT(*) as total_all,
                COUNT(CASE WHEN is_closed = FALSE THEN 1 END) as open_all,
                COUNT(CASE WHEN close_reason = 'take_profit' THEN 1 END) as tp_all,
                COUNT(CASE WHEN close_reason = 'stop_loss' THEN 1 END) as sl_all
            FROM web.web_signals
        """
        total_stats = db.execute_query(total_stats_query, fetch=True)[0]

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
                'saved_position_size': float(filters['position_size_usd'])
            },
            last_update=datetime.now()
        )

    except Exception as e:
        logger.error(f"Ошибка при загрузке страницы сигналов: {e}")
        import traceback
        logger.error(traceback.format_exc())
        flash('Ошибка при загрузке данных сигналов', 'error')
        return redirect(url_for('dashboard'))


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
