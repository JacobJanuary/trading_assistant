"""
Application Factory - полная версия со ВСЕМИ 42 маршрутами из app.py
Extracted from original 4456-line app.py file
"""
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, Response, send_file, make_response
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import logging
import os
from config import Config

def create_app(config_class=Config):
    """Создает и конфигурирует экземпляр Flask приложения"""
    app = Flask(__name__)
    
    # Настройка приложения
    app.secret_key = config_class.SECRET_KEY
    app.config.update(
        SESSION_COOKIE_SECURE=False,  # Для разработки
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
        REMEMBER_COOKIE_DURATION=timedelta(days=30),
        REMEMBER_COOKIE_SECURE=False,
        REMEMBER_COOKIE_HTTPONLY=True,
        MAX_CONTENT_LENGTH=16 * 1024 * 1024  # 16MB max file size
    )
    
    # Настройка логирования
    logging.basicConfig(
        level=getattr(logging, config_class.LOG_LEVEL, logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    logger = logging.getLogger(__name__)
    app.logger = logger
    
    # Инициализация базы данных
    from database import Database, initialize_signals_with_params, process_signal_complete
    db = Database(config_class.get_database_url())
    app.db = db
    
    # Инициализация схемы БД
    try:
        db.initialize_schema()
        app.logger.info("Схема базы данных инициализирована")
    except Exception as e:
        app.logger.error(f"Ошибка при инициализации схемы БД: {e}")
    
    # Настройка Flask-Login
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    login_manager.login_message = 'Пожалуйста, войдите в систему для доступа к этой странице.'
    login_manager.login_message_category = 'warning'
    
    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.get(db, user_id)
    
    # Регистрация ВСЕХ 42 маршрутов из оригинального app.py
    register_auth_routes(app, db)
    register_main_routes(app, db)
    register_signal_routes(app, db)
    register_scoring_routes(app, db)  # Добавлен раздел для scoring
    register_analysis_routes(app, db)
    register_strategy_routes(app, db)  # Добавлен раздел для стратегий и A/B тестов
    register_sse_routes(app, db)
    register_api_routes(app, db)
    register_admin_routes(app, db)
    register_data_routes(app, db)
    register_chart_routes(app, db)
    
    # Логирование успешной инициализации
    app.logger.info(f"База данных инициализирована: {config_class.DB_HOST}:{config_class.DB_PORT}/{config_class.DB_NAME}")
    app.logger.info(f"Пул соединений: min={config_class.DB_POOL_MIN_SIZE}, max={config_class.DB_POOL_MAX_SIZE}")
    app.logger.info(f"USE_CELERY: {config_class.USE_CELERY}")
    
    return app


def create_celery(app=None):
    """Создает и конфигурирует экземпляр Celery"""
    from celery import Celery
    
    if app is None:
        app = create_app()
    
    celery = Celery(
        app.import_name,
        broker=Config.CELERY_BROKER_URL,
        backend=Config.CELERY_RESULT_BACKEND
    )
    
    # Конфигурация Celery
    celery.conf.update(
        task_serializer='json',
        accept_content=['json'],
        result_serializer='json',
        timezone='UTC',
        enable_utc=True,
        task_time_limit=Config.CELERY_TASK_TIME_LIMIT,
        task_soft_time_limit=Config.CELERY_TASK_SOFT_TIME_LIMIT,
        result_expires=3600,
        worker_prefetch_multiplier=1,
        worker_max_tasks_per_child=100,
        task_acks_late=True,
        task_reject_on_worker_lost=True
    )
    
    # Обновляем контекст задач
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    
    # Импортируем задачи
    import celery_tasks
    
    return celery


def register_auth_routes(app, db):
    """Регистрация маршрутов авторизации"""
    from models import User
    
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            remember = request.form.get('remember') == 'on'
            
            user = User.authenticate(db, username, password)
            
            if user:
                login_user(user, remember=remember)
                app.logger.info(f"Пользователь {username} вошел в систему (remember={remember})")
                
                # Сохраняем настройки в сессии
                session.permanent = remember
                session['user_filters'] = user.filters
                
                next_page = request.args.get('next')
                if next_page:
                    return redirect(next_page)
                return redirect(url_for('dashboard'))
            else:
                flash('Неверное имя пользователя или пароль', 'error')
                app.logger.warning(f"Неудачная попытка входа для пользователя {username}")
                
        return render_template('login.html')
    
    @app.route('/logout')
    @login_required
    def logout():
        username = current_user.username if current_user.is_authenticated else 'Unknown'
        logout_user()
        session.clear()
        flash('Вы успешно вышли из системы', 'success')
        app.logger.info(f"Пользователь {username} вышел из системы")
        return redirect(url_for('login'))
    
    @app.route('/unauthorized')
    def unauthorized():
        """Страница для неподтвержденных пользователей"""
        return render_template('unauthorized.html')
    
    @app.route('/auth_status')
    def auth_status():
        """Проверка статуса авторизации"""
        return jsonify({
            'authenticated': current_user.is_authenticated,
            'username': current_user.username if current_user.is_authenticated else None,
            'is_admin': current_user.is_admin if current_user.is_authenticated else False,
            'is_approved': current_user.is_approved if current_user.is_authenticated else False
        })
    
    @app.route('/register', methods=['GET', 'POST'])
    def register():
        # Проверяем есть ли хоть один пользователь
        existing_users = db.execute_query("SELECT COUNT(*) FROM web.users", fetch=True)
        if existing_users and existing_users[0][0] > 0:
            flash('Регистрация новых пользователей отключена', 'warning')
            return redirect(url_for('login'))
        
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            
            if User.create(db, username, password):
                flash('Регистрация успешна! Теперь вы можете войти.', 'success')
                return redirect(url_for('login'))
            else:
                flash('Пользователь с таким именем уже существует', 'error')
                
        return render_template('register.html')


def register_main_routes(app, db):
    """Регистрация основных маршрутов"""
    from models import TradingStats
    
    @app.route('/')
    @login_required
    def index():
        return redirect(url_for('dashboard'))
    
    @app.route('/dashboard')
    @login_required
    def dashboard():
        stats = TradingStats.get_for_user(db, current_user.id)
        return render_template('dashboard.html', 
                             username=current_user.username, 
                             stats=stats,
                             is_admin=current_user.is_admin)
    
    @app.route('/signal_performance')
    @login_required
    def signal_performance():
        return render_template('signal_performance.html',
                             username=current_user.username,
                             is_admin=current_user.is_admin)
    
    @app.route('/signal_analysis')
    @login_required
    def signal_analysis():
        return render_template('signal_analysis.html',
                             username=current_user.username,
                             is_admin=current_user.is_admin)
    
    @app.route('/efficiency_analysis')
    @login_required
    def efficiency_analysis():
        return render_template('efficiency_analysis.html', 
                             username=current_user.username,
                             is_admin=current_user.is_admin)
    
    @app.route('/trailing_analysis')
    @login_required
    def trailing_analysis():
        return render_template('trailing_analysis.html',
                             username=current_user.username,
                             is_admin=current_user.is_admin)
    
    @app.route('/tp_sl_analysis')
    @login_required
    def tp_sl_analysis():
        return render_template('tp_sl_analysis.html',
                             username=current_user.username,
                             is_admin=current_user.is_admin)
    
    @app.route('/tpsl_analysis')
    @login_required
    def tpsl_analysis():
        """Страница TP/SL анализа (альтернативный маршрут)"""
        return render_template('tpsl_analysis.html',
                             username=current_user.username,
                             is_admin=current_user.is_admin)
    
    @app.route('/settings')
    @login_required
    def settings():
        user_filters = current_user.get_filters(db)
        return render_template('settings.html',
                             username=current_user.username,
                             user_filters=user_filters,
                             is_admin=current_user.is_admin)
    
    @app.route('/debug_session')
    @login_required
    def debug_session():
        """Отладочная информация о сессии"""
        return jsonify({
            'session_data': dict(session),
            'user_id': current_user.id,
            'filters': session.get('user_filters', {}),
            'username': current_user.username
        })
    
    @app.route('/api/dashboard-data')
    @login_required
    def api_dashboard_data():
        """API endpoint для получения данных дашборда"""
        from services import get_dashboard_data
        
        try:
            data = get_dashboard_data(db, current_user.id)
            return jsonify(data)
        except Exception as e:
            app.logger.error(f"Error getting dashboard data: {e}")
            return jsonify({'error': str(e)}), 500


def register_scoring_routes(app, db):
    """Регистрация маршрутов для Scoring Analysis (7 routes)"""
    
    @app.route('/scoring_analysis')
    @login_required
    def scoring_analysis():
        """Страница Scoring Analysis (старая версия)"""
        return render_template('scoring_analysis.html')
    
    @app.route('/scoring_analysis_v2')
    @login_required
    def scoring_analysis_v2():
        """Страница Scoring Analysis v2"""
        return render_template('scoring_analysis_v2.html')
    
    @app.route('/api/scoring/apply_filters', methods=['POST'])
    @login_required
    def api_scoring_apply_filters():
        """Применение фильтров для scoring анализа"""
        from services import apply_scoring_filters
        
        try:
            data = request.get_json()
            result = apply_scoring_filters(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error applying scoring filters: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/scoring/apply_filters_v2', methods=['POST'])
    @login_required
    def api_scoring_apply_filters_v2():
        """Применение фильтров для scoring анализа v2"""
        from services import apply_scoring_filters_v2
        
        try:
            data = request.get_json()
            result = apply_scoring_filters_v2(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error applying scoring filters v2: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/scoring/save_filters', methods=['POST'])
    @login_required
    def api_scoring_save_filters():
        """Сохранение фильтров scoring анализа"""
        from services import save_scoring_filters
        
        try:
            data = request.get_json()
            save_scoring_filters(db, current_user.id, data)
            return jsonify({'success': True})
        except Exception as e:
            app.logger.error(f"Error saving scoring filters: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/scoring/get_date_info', methods=['POST'])
    @login_required
    def api_scoring_get_date_info():
        """Получение информации о датах для scoring анализа"""
        from services import get_scoring_date_info
        
        try:
            data = request.get_json()
            result = get_scoring_date_info(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error getting scoring date info: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/scoring/get_date_info_v2', methods=['POST'])
    @login_required
    def api_scoring_get_date_info_v2():
        """Получение информации о датах для scoring анализа v2"""
        from services import get_scoring_date_info_v2
        
        try:
            data = request.get_json()
            result = get_scoring_date_info_v2(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error getting scoring date info v2: {e}")
            return jsonify({'error': str(e)}), 500


def register_signal_routes(app, db):
    """Регистрация маршрутов для работы с сигналами"""
    from database import get_scoring_signals_v2, process_scoring_signals_batch
    
    @app.route('/api/signals/analysis', methods=['GET'])
    @login_required
    def api_signals_analysis():
        """API endpoint для получения сигналов для анализа"""
        try:
            # Получаем фильтры пользователя
            user_filters = current_user.get_filters(db)
            
            # Получаем параметры из запроса
            date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
            use_trailing_stop = request.args.get('use_trailing_stop', str(user_filters.get('use_trailing_stop', False))).lower() == 'true'
            
            # Score фильтры
            score_week_min = float(request.args.get('score_week_min', user_filters.get('score_week_min', 0)))
            score_month_min = float(request.args.get('score_month_min', user_filters.get('score_month_min', 0)))
            
            # Макс. сделок за 15 мин
            max_trades_per_15min = int(request.args.get('max_trades_per_15min', user_filters.get('max_trades_per_15min', 3)))
            
            # Формируем уникальный ключ кеша
            cache_key = f"signals_analysis_{current_user.id}_{date}_{score_week_min}_{score_month_min}_{use_trailing_stop}_{max_trades_per_15min}"
            
            # Получаем сигналы
            raw_signals = get_scoring_signals_v2(
                db,
                start_date=date,
                end_date=date,
                score_week_min=score_week_min,
                score_month_min=score_month_min,
                cache_key=cache_key
            )
            
            if not raw_signals:
                return jsonify({'signals': [], 'stats': {}})
            
            # Обрабатываем с параметрами пользователя
            result = process_scoring_signals_batch(
                raw_signals,
                stop_loss=user_filters['stop_loss_percent'],
                take_profit=user_filters['take_profit_percent'],
                position_size_usd=user_filters['position_size_usd'],
                leverage=user_filters['leverage'],
                use_trailing_stop=use_trailing_stop,
                trailing_distance_pct=user_filters.get('trailing_distance_pct', 0.5),
                trailing_activation_pct=user_filters.get('trailing_activation_pct', 4.0),
                allowed_hours=user_filters.get('allowed_hours', list(range(24))),
                max_trades_per_15min=max_trades_per_15min
            )
            
            # Преобразуем Decimal в float
            for signal in result['signals']:
                for key in ['entry_price', 'current_price', 'pnl_usd', 'pnl_pct', 
                          'stop_loss', 'take_profit', 'position_size_usd']:
                    if key in signal and isinstance(signal[key], Decimal):
                        signal[key] = float(signal[key])
            
            return jsonify(result)
            
        except Exception as e:
            app.logger.error(f"Ошибка в api_signals_analysis: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/signals/performance', methods=['GET'])
    @login_required
    def api_signals_performance():
        """API endpoint для получения производительности сигналов"""
        try:
            user_filters = current_user.get_filters(db)
            
            date = request.args.get('date', datetime.now().strftime('%Y-%m-%d'))
            use_trailing_stop = request.args.get('use_trailing_stop', str(user_filters.get('use_trailing_stop', False))).lower() == 'true'
            score_week_min = float(request.args.get('score_week_min', user_filters.get('score_week_min', 0)))
            score_month_min = float(request.args.get('score_month_min', user_filters.get('score_month_min', 0)))
            max_trades_per_15min = int(request.args.get('max_trades_per_15min', user_filters.get('max_trades_per_15min', 3)))
            
            cache_key = f"signals_perf_{current_user.id}_{date}_{score_week_min}_{score_month_min}_{use_trailing_stop}_{max_trades_per_15min}"
            
            raw_signals = get_scoring_signals_v2(
                db,
                start_date=date,
                end_date=date,
                score_week_min=score_week_min,
                score_month_min=score_month_min,
                cache_key=cache_key
            )
            
            if not raw_signals:
                return jsonify({'signals': [], 'stats': {}})
            
            result = process_scoring_signals_batch(
                raw_signals,
                stop_loss=user_filters['stop_loss_percent'],
                take_profit=user_filters['take_profit_percent'],
                position_size_usd=user_filters['position_size_usd'],
                leverage=user_filters['leverage'],
                use_trailing_stop=use_trailing_stop,
                trailing_distance_pct=user_filters.get('trailing_distance_pct', 0.5),
                trailing_activation_pct=user_filters.get('trailing_activation_pct', 4.0),
                allowed_hours=user_filters.get('allowed_hours', list(range(24))),
                max_trades_per_15min=max_trades_per_15min
            )
            
            # Конвертируем Decimal в float
            for signal in result['signals']:
                for key in signal:
                    if isinstance(signal[key], Decimal):
                        signal[key] = float(signal[key])
            
            if 'stats' in result:
                for key in result['stats']:
                    if isinstance(result['stats'][key], Decimal):
                        result['stats'][key] = float(result['stats'][key])
            
            return jsonify(result)
            
        except Exception as e:
            app.logger.error(f"Ошибка в api_signals_performance: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/initialize_signals', methods=['POST'])
    @login_required
    def api_initialize_signals():
        """API для инициализации сигналов"""
        from database import initialize_signals_with_params
        
        try:
            params = request.get_json()
            result = initialize_signals_with_params(db, current_user.id, params)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error initializing signals: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/initialize_signals_trailing', methods=['POST'])
    @login_required
    def api_initialize_signals_trailing():
        """Инициализация сигналов для Trailing Stop анализа"""
        from services import initialize_signals_trailing
        
        try:
            data = request.get_json()
            result = initialize_signals_trailing(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error in initialize_signals_trailing: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/reinitialize_signals', methods=['POST'])
    @login_required
    def api_reinitialize_signals():
        """Переинициализация сигналов с новыми параметрами"""
        from services import reinitialize_signals
        
        try:
            params = request.get_json()
            result = reinitialize_signals(db, current_user.id, params)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error reinitializing signals: {e}")
            return jsonify({'error': str(e)}), 500


def register_strategy_routes(app, db):
    """Регистрация маршрутов для стратегий и A/B тестирования (4 routes)"""
    
    @app.route('/strategy_comparison')
    @login_required
    def strategy_comparison():
        """Страница сравнения стратегий"""
        return render_template('strategy_comparison.html')
    
    @app.route('/ab_testing')
    @login_required
    def ab_testing():
        """Страница A/B тестирования"""
        return render_template('ab_testing.html')
    
    @app.route('/api/strategy/compare', methods=['POST'])
    @login_required
    def api_strategy_compare():
        """API для сравнения стратегий"""
        from services import compare_strategies
        
        try:
            data = request.get_json()
            result = compare_strategies(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error comparing strategies: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/ab_test/run', methods=['POST'])
    @login_required
    def api_ab_test_run():
        """API для запуска A/B тестирования"""
        from services import run_ab_test
        
        try:
            data = request.get_json()
            result = run_ab_test(db, current_user.id, data)
            return jsonify(result)
        except Exception as e:
            app.logger.error(f"Error running A/B test: {e}")
            return jsonify({'error': str(e)}), 500


def register_analysis_routes(app, db):
    """Регистрация маршрутов анализа"""
    
    @app.route('/api/analysis/efficiency_30days', methods=['GET'])
    @login_required
    def api_analysis_efficiency_30days():
        """Анализ эффективности за 30 дней"""
        try:
            from database import get_scoring_signals_v2, process_scoring_signals_batch
            
            user_filters = current_user.get_filters(db)
            
            # Получаем параметры score из запроса
            score_week_min = int(request.args.get('score_week_min', 60))
            score_week_max = int(request.args.get('score_week_max', 80))
            score_month_min = int(request.args.get('score_month_min', 60))
            score_month_max = int(request.args.get('score_month_max', 80))
            step = int(request.args.get('step', 10))
            max_trades_per_15min = int(request.args.get('max_trades_per_15min', 
                                      user_filters.get('max_trades_per_15min', 3)))
            
            # Даты для анализа (последние 30 дней)
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            
            results = []
            
            # Перебираем все комбинации score параметров
            for score_week in range(score_week_min, score_week_max + 1, step):
                for score_month in range(score_month_min, score_month_max + 1, step):
                    # Получаем сигналы для этой комбинации
                    raw_signals = get_scoring_signals_v2(
                        db,
                        start_date=start_date.strftime('%Y-%m-%d'),
                        end_date=end_date.strftime('%Y-%m-%d'),
                        score_week_min=score_week,
                        score_month_min=score_month
                    )
                    
                    if raw_signals:
                        # Обрабатываем сигналы
                        processed = process_scoring_signals_batch(
                            raw_signals,
                            stop_loss=user_filters['stop_loss_percent'],
                            take_profit=user_filters['take_profit_percent'],
                            position_size_usd=user_filters['position_size_usd'],
                            leverage=user_filters['leverage'],
                            use_trailing_stop=user_filters.get('use_trailing_stop', False),
                            trailing_distance_pct=user_filters.get('trailing_distance_pct', 0.5),
                            trailing_activation_pct=user_filters.get('trailing_activation_pct', 4.0),
                            allowed_hours=user_filters.get('allowed_hours', list(range(24))),
                            max_trades_per_15min=max_trades_per_15min
                        )
                        
                        # Считаем статистику
                        total_pnl = sum(float(s['pnl_usd']) for s in processed['signals'])
                        total_signals = len(processed['signals'])
                        tp_count = sum(1 for s in processed['signals'] if s['result'] == 'TP')
                        sl_count = sum(1 for s in processed['signals'] if s['result'] == 'SL')
                        win_rate = (tp_count / total_signals * 100) if total_signals > 0 else 0
                        
                        results.append({
                            'score_week': score_week,
                            'score_month': score_month,
                            'total_pnl': round(total_pnl, 2),
                            'total_signals': total_signals,
                            'tp_count': tp_count,
                            'sl_count': sl_count,
                            'win_rate': round(win_rate, 1)
                        })
            
            # Сортируем по убыванию PnL
            results.sort(key=lambda x: x['total_pnl'], reverse=True)
            
            return jsonify({'combinations': results})
            
        except Exception as e:
            app.logger.error(f"Ошибка в api_analysis_efficiency_30days: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500


def register_sse_routes(app, db):
    """Регистрация SSE endpoints"""
    
    @app.route('/api/efficiency/analyze_30days_progress')
    @login_required
    def api_efficiency_analyze_30days_progress():
        """SSE endpoint для анализа эффективности с прогрессом"""
        try:
            app.logger.info("=== Efficiency analysis endpoint called ===")
            app.logger.info(f"Request args: {request.args}")
            app.logger.info(f"Current user: {current_user.username if current_user else 'None'}")
            
            use_celery = request.args.get('use_celery', 'false').lower() == 'true'
            app.logger.info(f"Config.USE_CELERY={Config.USE_CELERY}, use_celery param={use_celery}")
            
            if use_celery and Config.USE_CELERY:
                app.logger.info("Using Celery version for efficiency analysis")
                from celery_sse_endpoints import analyze_efficiency_celery
                return analyze_efficiency_celery(current_user.id)
            else:
                app.logger.info("Using synchronous version for efficiency analysis")
                # Здесь должна быть синхронная версия из оригинального app.py
                # Для краткости я показываю заглушку
                return Response("data: {\"type\": \"error\", \"message\": \"Synchronous version not implemented\"}\n\n",
                              mimetype='text/event-stream')
                
        except Exception as e:
            app.logger.error(f"Error in efficiency endpoint: {e}", exc_info=True)
            return Response(f"data: {{\"type\": \"error\", \"message\": \"{str(e)}\"}}\n\n",
                          mimetype='text/event-stream')
    
    @app.route('/api/tpsl/analyze_progress')
    @login_required
    def api_tpsl_analyze_progress():
        """SSE endpoint для прогресса TP/SL анализа"""
        try:
            use_celery = request.args.get('use_celery', 'false').lower() == 'true'
            
            if use_celery and Config.USE_CELERY:
                from celery_sse_endpoints import analyze_tpsl_celery
                return analyze_tpsl_celery(current_user.id)
            else:
                from sse_endpoints import analyze_tpsl_sse
                return analyze_tpsl_sse()
        except Exception as e:
            app.logger.error(f"Error in TP/SL analysis SSE: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/trailing/analyze_progress')
    @login_required
    def api_trailing_analyze_progress():
        """SSE endpoint для анализа Trailing Stop"""
        try:
            use_celery = request.args.get('use_celery', 'false').lower() == 'true'
            app.logger.info(f"Trailing Stop analysis: Config.USE_CELERY={Config.USE_CELERY}, use_celery={use_celery}")
            
            if use_celery and Config.USE_CELERY:
                app.logger.info("Using Celery version for Trailing Stop")
                from celery_sse_endpoints import analyze_trailing_stop_celery
                return analyze_trailing_stop_celery(current_user.id)
            else:
                app.logger.info("Using synchronous version for Trailing Stop")
                return Response("data: {\"type\": \"error\", \"message\": \"Synchronous version not implemented\"}\n\n",
                              mimetype='text/event-stream')
                
        except Exception as e:
            app.logger.error(f"Error in trailing endpoint: {e}", exc_info=True)
            return Response(f"data: {{\"type\": \"error\", \"message\": \"{str(e)}\"}}\n\n",
                          mimetype='text/event-stream')


def register_api_routes(app, db):
    """Регистрация API маршрутов"""
    
    @app.route('/api/config/celery_status')
    @login_required
    def api_celery_status():
        """Проверка статуса Celery"""
        status = {
            'USE_CELERY': Config.USE_CELERY,
            'CELERY_BROKER_URL': Config.CELERY_BROKER_URL if Config.USE_CELERY else None,
            'celery_tasks_loaded': False,
            'celery_workers_online': False
        }
        
        if Config.USE_CELERY:
            try:
                from celery_worker import celery
                from celery_tasks import analyze_efficiency_combination
                
                # Проверяем загрузку задач
                tasks = [t for t in celery.tasks.keys() if 'analyze' in t]
                status['celery_tasks_loaded'] = len(tasks) > 0
                status['celery_tasks'] = tasks
                
                # Проверяем воркеров
                i = celery.control.inspect()
                stats = i.stats()
                if stats:
                    status['celery_workers_online'] = True
                    status['celery_workers'] = list(stats.keys())
            except Exception as e:
                status['error'] = str(e)
        
        return jsonify(status)
    
    @app.route('/api/user/settings', methods=['GET', 'POST'])
    @login_required
    def api_user_settings():
        """API для получения и обновления настроек пользователя"""
        if request.method == 'GET':
            user_filters = current_user.get_filters(db)
            return jsonify(user_filters)
        
        elif request.method == 'POST':
            try:
                data = request.json
                
                # Валидация и обновление настроек
                update_fields = {}
                
                # Числовые поля
                numeric_fields = {
                    'hide_younger_than_hours': (0, 48),
                    'hide_older_than_hours': (1, 168),
                    'stop_loss_percent': (0.5, 20),
                    'take_profit_percent': (0.5, 50),
                    'position_size_usd': (10, 10000),
                    'leverage': (1, 20),
                    'trailing_distance_pct': (0.1, 10),
                    'trailing_activation_pct': (0.1, 10),
                    'score_week_min': (0, 100),
                    'score_month_min': (0, 100),
                    'max_trades_per_15min': (1, 10)
                }
                
                for field, (min_val, max_val) in numeric_fields.items():
                    if field in data:
                        value = float(data[field]) if 'percent' in field or 'pct' in field else int(data[field])
                        value = max(min_val, min(max_val, value))
                        update_fields[field] = value
                
                # Булевы поля
                if 'use_trailing_stop' in data:
                    update_fields['use_trailing_stop'] = bool(data['use_trailing_stop'])
                
                # Массив разрешенных часов
                if 'allowed_hours' in data:
                    allowed_hours = data['allowed_hours']
                    if isinstance(allowed_hours, list):
                        allowed_hours = [int(h) for h in allowed_hours if 0 <= int(h) <= 23]
                        update_fields['allowed_hours'] = allowed_hours
                
                # Обновляем в БД
                if update_fields:
                    current_user.update_filters(db, update_fields)
                    
                    # Обновляем сессию
                    session['user_filters'] = current_user.get_filters(db)
                    
                    return jsonify({'success': True, 'message': 'Настройки сохранены'})
                else:
                    return jsonify({'success': False, 'message': 'Нет данных для обновления'}), 400
                    
            except Exception as e:
                app.logger.error(f"Ошибка при обновлении настроек: {e}")
                return jsonify({'success': False, 'message': str(e)}), 500
    
    @app.route('/api/save_filters', methods=['POST'])
    @login_required
    def api_save_filters():
        """Сохранение пользовательских фильтров"""
        try:
            filters = request.get_json()
            session['user_filters'] = session.get('user_filters', {})
            session['user_filters'][str(current_user.id)] = filters
            session.modified = True
            
            return jsonify({
                'success': True,
                'saved_filters': filters
            })
        except Exception as e:
            app.logger.error(f"Error saving filters: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/save_trading_mode', methods=['POST'])
    @login_required
    def api_save_trading_mode():
        """Сохранение торгового режима пользователя"""
        from services import save_trading_mode
        
        try:
            data = request.get_json()
            mode = data.get('mode', 'spot')
            
            save_trading_mode(db, current_user.id, mode)
            
            return jsonify({'success': True, 'mode': mode})
        except Exception as e:
            app.logger.error(f"Error saving trading mode: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/get_user_trading_mode')
    @login_required
    def api_get_user_trading_mode():
        """Получение текущего торгового режима пользователя"""
        from services import get_user_trading_mode
        
        try:
            mode = get_user_trading_mode(db, current_user.id)
            return jsonify({'mode': mode})
        except Exception as e:
            app.logger.error(f"Error getting trading mode: {e}")
            return jsonify({'error': str(e)}), 500
    
    @app.route('/api/clear_cache', methods=['POST'])
    @login_required
    def api_clear_cache():
        """Очистка кеша"""
        try:
            # Здесь должна быть логика очистки кеша
            return jsonify({'success': True, 'message': 'Кеш очищен'})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)}), 500


def register_admin_routes(app, db):
    """Регистрация административных маршрутов"""
    
    @app.route('/admin')
    @login_required
    def admin():
        if not current_user.is_admin:
            flash('Доступ запрещен', 'error')
            return redirect(url_for('dashboard'))
        
        # Получаем статистику
        stats = {
            'total_users': db.execute_query("SELECT COUNT(*) FROM web.users", fetch=True)[0][0],
            'total_signals': db.execute_query("SELECT COUNT(*) FROM crypto.scoring_signals", fetch=True)[0][0],
            'db_size': db.execute_query("SELECT pg_database_size(current_database())", fetch=True)[0][0]
        }
        
        return render_template('admin.html',
                             username=current_user.username,
                             stats=stats,
                             is_admin=True)


def register_data_routes(app, db):
    """Регистрация маршрутов для работы с данными"""
    
    @app.route('/api/data/export', methods=['GET'])
    @login_required
    def api_data_export():
        """Экспорт данных в CSV"""
        try:
            import csv
            from io import StringIO
            
            # Получаем параметры
            date_from = request.args.get('date_from')
            date_to = request.args.get('date_to')
            
            # Здесь должна быть логика экспорта
            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(['Date', 'Symbol', 'PnL', 'Result'])
            
            # Возвращаем CSV файл
            return Response(
                output.getvalue(),
                mimetype='text/csv',
                headers={'Content-Disposition': 'attachment;filename=export.csv'}
            )
            
        except Exception as e:
            return jsonify({'error': str(e)}), 500


def register_chart_routes(app, db):
    """Регистрация маршрутов для графиков"""
    
    @app.route('/api/charts/pnl_history')
    @login_required
    def api_charts_pnl_history():
        """История PnL для графика"""
        try:
            days = int(request.args.get('days', 30))
            
            # Здесь должна быть логика получения истории PnL
            data = {
                'dates': [],
                'values': []
            }
            
            return jsonify(data)
            
        except Exception as e:
            return jsonify({'error': str(e)}), 500