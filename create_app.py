"""
Application Factory для правильной инициализации Flask и Celery
"""
from flask import Flask
from flask_login import LoginManager
from celery import Celery
from config import Config
import logging

def create_app(config_class=Config):
    """Создает и конфигурирует экземпляр Flask приложения"""
    app = Flask(__name__)
    
    # Применяем конфигурацию
    app.config['SECRET_KEY'] = config_class.SECRET_KEY
    app.config['PERMANENT_SESSION_LIFETIME'] = config_class.PERMANENT_SESSION_LIFETIME
    app.config['DEBUG'] = config_class.FLASK_DEBUG
    
    # Настройка логирования
    logging.basicConfig(
        level=getattr(logging, config_class.LOG_LEVEL, logging.INFO),
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Инициализация LoginManager
    login_manager = LoginManager()
    login_manager.init_app(app)
    login_manager.login_view = 'login'
    
    # Инициализация базы данных
    from database import Database
    db = Database(config_class.get_database_url())
    app.db = db
    
    # Регистрация функции загрузки пользователя
    @login_manager.user_loader
    def load_user(user_id):
        from models import User
        return User.get(db, user_id)
    
    # Регистрация всех маршрутов
    register_routes(app)
    
    # Логирование успешной инициализации
    app.logger.info(f"Приложение инициализировано: {config_class.DB_HOST}:{config_class.DB_PORT}/{config_class.DB_NAME}")
    app.logger.info(f"USE_CELERY: {config_class.USE_CELERY}")
    
    return app

def create_celery(app=None):
    """Создает и конфигурирует экземпляр Celery"""
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
    
    # Обновляем контекст задач чтобы они имели доступ к app
    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)
    
    celery.Task = ContextTask
    
    # Регистрируем задачи
    from celery_tasks import (
        analyze_efficiency_combination,
        analyze_trailing_stop_combination,
        analyze_tp_sl_combination
    )
    
    return celery

def register_routes(app):
    """Регистрирует все маршруты приложения"""
    from flask import render_template, request, redirect, url_for, jsonify, flash, session, Response, send_file, make_response
    from flask_login import login_user, logout_user, login_required, current_user
    from werkzeug.security import check_password_hash
    from datetime import datetime, timedelta
    from config import Config
    import json
    
    # === АВТОРИЗАЦИЯ ===
    @app.route('/login', methods=['GET', 'POST'])
    def login():
        if request.method == 'POST':
            username = request.form.get('username')
            password = request.form.get('password')
            remember = request.form.get('remember') == 'on'
            
            from models import User
            user = User.authenticate(app.db, username, password)
            
            if user:
                login_user(user, remember=remember)
                app.logger.info(f"Пользователь {username} вошел в систему")
                next_page = request.args.get('next')
                return redirect(next_page or url_for('dashboard'))
            else:
                flash('Неверное имя пользователя или пароль', 'error')
                
        return render_template('login.html')
    
    @app.route('/logout')
    @login_required
    def logout():
        username = current_user.username
        logout_user()
        app.logger.info(f"Пользователь {username} вышел из системы")
        return redirect(url_for('login'))
    
    # === ОСНОВНЫЕ СТРАНИЦЫ ===
    @app.route('/')
    @login_required
    def index():
        return redirect(url_for('dashboard'))
    
    @app.route('/dashboard')
    @login_required
    def dashboard():
        from models import TradingStats
        stats = TradingStats.get_for_user(app.db, current_user.id)
        return render_template('dashboard.html', 
                             username=current_user.username, 
                             stats=stats,
                             is_admin=current_user.is_admin)
    
    # === SSE ENDPOINTS ===
    @app.route('/api/efficiency/analyze_30days_progress')
    @login_required
    def api_efficiency_analyze_30days_progress():
        """SSE endpoint для анализа эффективности"""
        try:
            use_celery = request.args.get('use_celery', 'false').lower() == 'true'
            
            if use_celery and Config.USE_CELERY:
                from celery_sse_endpoints import analyze_efficiency_celery
                return analyze_efficiency_celery(current_user.id)
            else:
                # Fallback на синхронную версию
                return Response("data: {\"type\": \"error\", \"message\": \"Celery disabled\"}\n\n", 
                              mimetype='text/event-stream')
                
        except Exception as e:
            app.logger.error(f"Error in efficiency SSE: {e}", exc_info=True)
            return Response(f"data: {{\"type\": \"error\", \"message\": \"{str(e)}\"}}\n\n", 
                          mimetype='text/event-stream')
    
    @app.route('/api/trailing/analyze_progress')
    @login_required
    def api_trailing_analyze_progress():
        """SSE endpoint для анализа Trailing Stop"""
        try:
            use_celery = request.args.get('use_celery', 'false').lower() == 'true'
            
            if use_celery and Config.USE_CELERY:
                from celery_sse_endpoints import analyze_trailing_stop_celery
                return analyze_trailing_stop_celery(current_user.id)
            else:
                return Response("data: {\"type\": \"error\", \"message\": \"Celery disabled\"}\n\n", 
                              mimetype='text/event-stream')
                
        except Exception as e:
            app.logger.error(f"Error in trailing SSE: {e}", exc_info=True)
            return Response(f"data: {{\"type\": \"error\", \"message\": \"{str(e)}\"}}\n\n", 
                          mimetype='text/event-stream')
    
    # === СТРАНИЦЫ АНАЛИЗА ===
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
    
    # Добавьте остальные маршруты из оригинального app.py здесь
    # Я показал основные для примера