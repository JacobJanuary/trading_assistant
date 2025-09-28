"""
API для управления анализами через таблицу analysis_tasks
"""
from flask import jsonify, request
from flask_login import current_user, login_required
from database import Database
from celery_efficiency_parallel import analyze_efficiency_parallel as analyze_efficiency_no_deadlock
from celery_analysis_tasks import analyze_trailing_stop, analyze_tpsl_optimization
# Используем новый модуль без deadlock для обеих версий
analyze_efficiency_parallel = analyze_efficiency_no_deadlock
analyze_efficiency_30days = analyze_efficiency_no_deadlock
import json
import logging

logger = logging.getLogger(__name__)

def create_or_get_active_task(db, user_id, task_type):
    """Проверяет наличие активной задачи и возвращает ее или None"""
    result = db.execute_query("""
        SELECT task_id, status, progress_current, progress_total, progress_percent, progress_status, result_data
        FROM web.analysis_tasks
        WHERE user_id = %s AND task_type = %s AND status IN ('running', 'completed')
        ORDER BY created_at DESC
        LIMIT 1
    """, (user_id, task_type), fetch=True)

    if result and len(result) > 0:
        row = result[0]
        return {
            'task_id': row['task_id'] if isinstance(row, dict) else row[0],
            'status': row['status'] if isinstance(row, dict) else row[1],
            'progress_current': row['progress_current'] if isinstance(row, dict) else row[2],
            'progress_total': row['progress_total'] if isinstance(row, dict) else row[3],
            'progress_percent': row['progress_percent'] if isinstance(row, dict) else row[4],
            'progress_status': row['progress_status'] if isinstance(row, dict) else row[5],
            'result_data': row['result_data'] if isinstance(row, dict) else row[6]
        }
    return None

def create_task_record(db, user_id, task_type, task_id):
    """Создает новую запись о задаче"""
    db.execute_query("""
        INSERT INTO web.analysis_tasks
        (user_id, task_type, task_id, status, progress_current, progress_total, progress_percent, progress_status)
        VALUES (%s, %s, %s, 'running', 0, 0, 0, 'Инициализация...')
    """, (user_id, task_type, task_id))

def register_analysis_api_routes(app, db):
    """Регистрация новых API маршрутов для управления анализами"""

    # ===== Efficiency Analysis =====
    @app.route('/api/efficiency_analysis/start', methods=['POST'])
    def api_efficiency_analysis_start():
        """Запуск анализа эффективности"""
        try:
            # Временно используем user_id = 1 для тестирования
            user_id = 1

            # Проверяем наличие активной задачи
            active_task = create_or_get_active_task(db, user_id, 'efficiency_analysis')

            if active_task and active_task['status'] == 'running':
                return jsonify({
                    'success': False,
                    'error': 'Анализ уже запущен',
                    'task_id': active_task['task_id'],
                    'progress': active_task['progress_percent']
                })

            # Получаем параметры
            data = request.get_json()
            filters = {
                'score_week_min': data.get('score_week_min', 60),
                'score_week_max': data.get('score_week_max', 80),
                'score_month_min': data.get('score_month_min', 60),
                'score_month_max': data.get('score_month_max', 80),
                'step': data.get('step', 10),
                'max_trades_per_15min': data.get('max_trades_per_15min', 3),
                'take_profit_percent': data.get('take_profit_percent', 4.0),
                'stop_loss_percent': data.get('stop_loss_percent', 3.0),
                'position_size_usd': data.get('position_size_usd', 100),
                'leverage': data.get('leverage', 5),
                'use_trailing_stop': data.get('use_trailing_stop', False),
                'trailing_distance_pct': data.get('trailing_distance_pct', 2.0),
                'trailing_activation_pct': data.get('trailing_activation_pct', 1.0),
                'allowed_hours': data.get('allowed_hours', list(range(24)))
            }

            # Временно используем user_id = 1 для тестирования
            user_id = 1

            # Определяем количество комбинаций для выбора метода
            week_steps = range(filters['score_week_min'], filters['score_week_max'] + 1, filters['step'])
            month_steps = range(filters['score_month_min'], filters['score_month_max'] + 1, filters['step'])
            total_combinations = len(list(week_steps)) * len(list(month_steps))

            # Всегда используем параллельную версию
            task = analyze_efficiency_parallel.apply_async(args=[user_id, filters])
            logger.info(f"Using parallel analysis for {total_combinations} combinations")

            # Создаем запись в БД
            create_task_record(db, user_id, 'efficiency_analysis', task.id)

            return jsonify({
                'success': True,
                'task_id': task.id,
                'message': 'Анализ запущен'
            })

        except Exception as e:
            logger.error(f"Error starting efficiency analysis: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/efficiency_analysis/status')
    def api_efficiency_analysis_status():
        """Получение статуса анализа эффективности"""
        try:
            # Временно используем user_id = 1 для тестирования
            user_id = 1
            active_task = create_or_get_active_task(db, user_id, 'efficiency_analysis')

            if not active_task:
                return jsonify({
                    'success': True,
                    'status': 'idle',
                    'message': 'Нет активных задач'
                })

            # Проверяем, жива ли задача в Celery
            if active_task['status'] == 'running':
                from celery.result import AsyncResult
                from celery_efficiency_parallel import app as celery
                task_result = AsyncResult(active_task['task_id'], app=celery)

                # Проверяем основную задачу
                if task_result.state in ['PENDING', 'FAILURE', 'REVOKED']:
                    # Помечаем как сбойную
                    db.execute_query("""
                        UPDATE web.analysis_tasks
                        SET status = 'stalled',
                            error_message = 'Задача была прервана или потеряна. Пожалуйста, запустите анализ заново.',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE task_id = %s
                    """, (active_task['task_id'],))

                    return jsonify({
                        'success': True,
                        'task_id': active_task['task_id'],
                        'status': 'stalled',
                        'error': 'Задача была прервана. Пожалуйста, запустите анализ заново.',
                        'can_restart': True
                    })

                # Для параллельных задач проверяем chord
                if task_result.state == 'SUCCESS' and task_result.result:
                    result = task_result.result
                    if isinstance(result, dict) and 'chord_id' in result:
                        # Это параллельная задача, проверяем chord
                        chord_task = AsyncResult(result['chord_id'], app=celery)

                        # Проверяем статус chord
                        if chord_task.state == 'FAILURE':
                            # Chord упал с ошибкой
                            error_msg = str(chord_task.info) if chord_task.info else 'Ошибка выполнения параллельных задач'

                            db.execute_query("""
                                UPDATE web.analysis_tasks
                                SET status = 'failed',
                                    error_message = %s,
                                    updated_at = CURRENT_TIMESTAMP
                                WHERE task_id = %s
                            """, (error_msg, active_task['task_id']))

                            return jsonify({
                                'success': True,
                                'task_id': active_task['task_id'],
                                'status': 'failed',
                                'error': error_msg,
                                'can_restart': True
                            })

                        elif chord_task.state == 'SUCCESS':
                            # Chord завершен, но результат может быть пустым
                            # Это обработается в результатах
                            pass

            return jsonify({
                'success': True,
                'task_id': active_task['task_id'],
                'status': active_task['status'],
                'progress': {
                    'current': active_task['progress_current'],
                    'total': active_task['progress_total'],
                    'percent': active_task['progress_percent'],
                    'status': active_task['progress_status']
                },
                'has_result': active_task['status'] == 'completed' and active_task['result_data'] is not None
            })

        except Exception as e:
            logger.error(f"Error getting efficiency analysis status: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/efficiency_analysis/result')
    def api_efficiency_analysis_result():
        """Получение результата анализа эффективности"""
        try:
            # Временно используем user_id = 1 для тестирования
            user_id = 1
            active_task = create_or_get_active_task(db, user_id, 'efficiency_analysis')

            if not active_task:
                return jsonify({
                    'success': False,
                    'error': 'Результаты не найдены'
                })

            if active_task['status'] != 'completed':
                return jsonify({
                    'success': False,
                    'error': 'Анализ еще не завершен',
                    'status': active_task['status']
                })

            # result_data уже dict из JSONB поля БД
            result_data = active_task['result_data']

            if result_data:
                # Возвращаем сами данные, они уже содержат success
                return jsonify(result_data)
            else:
                return jsonify({
                    'success': False,
                    'error': 'Результаты не найдены'
                })

        except Exception as e:
            logger.error(f"Error getting efficiency analysis result: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/efficiency_analysis/cancel', methods=['POST'])
    def api_efficiency_analysis_cancel():
        """Отмена текущего анализа эффективности"""
        try:
            # Временно используем user_id = 1 для тестирования
            user_id = 1

            # Получаем активные задачи для отмены
            active_tasks = db.fetch_query("""
                SELECT task_id
                FROM web.analysis_tasks
                WHERE user_id = %s
                AND task_type = 'efficiency_analysis'
                AND status IN ('running', 'stalled')
            """, (user_id,))

            # Отменяем Celery задачи
            from celery.result import AsyncResult
            from celery_analysis_tasks import celery

            for task in active_tasks:
                try:
                    task_id = task['task_id']
                    celery_task = AsyncResult(task_id, app=celery)

                    # Пытаемся отменить задачу
                    celery_task.revoke(terminate=True, signal='SIGKILL')
                    logger.info(f"Revoked Celery task {task_id}")

                    # Если это была параллельная задача, проверяем chord_id
                    if celery_task.ready() and celery_task.result:
                        result = celery_task.result
                        if isinstance(result, dict) and 'chord_id' in result:
                            chord_task = AsyncResult(result['chord_id'], app=celery)
                            chord_task.revoke(terminate=True, signal='SIGKILL')
                            logger.info(f"Revoked chord task {result['chord_id']}")

                except Exception as e:
                    logger.error(f"Error revoking task {task['task_id']}: {e}")

            # Помечаем все активные задачи как отмененные в БД
            db.execute_query("""
                UPDATE web.analysis_tasks
                SET status = 'cancelled',
                    error_message = 'Отменено пользователем',
                    updated_at = CURRENT_TIMESTAMP
                WHERE user_id = %s
                AND task_type = 'efficiency_analysis'
                AND status IN ('running', 'stalled')
            """, (user_id,))

            return jsonify({
                'success': True,
                'message': 'Анализ отменен'
            })

        except Exception as e:
            logger.error(f"Error cancelling efficiency analysis: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/efficiency_analysis/clear', methods=['POST'])
    def api_efficiency_analysis_clear():
        """Очистка результатов анализа эффективности"""
        try:
            # Временно используем user_id = 1 для тестирования
            user_id = 1

            db.execute_query("""
                DELETE FROM web.analysis_tasks
                WHERE user_id = %s AND task_type = 'efficiency_analysis'
            """, (user_id,))

            return jsonify({
                'success': True,
                'message': 'История анализов очищена'
            })

        except Exception as e:
            logger.error(f"Error clearing efficiency analysis: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # ===== TP/SL Analysis =====
    @app.route('/api/tpsl_analysis/start', methods=['POST'])
    @login_required
    def api_tpsl_analysis_start():
        """Запуск анализа TP/SL"""
        try:
            # Проверяем наличие активной задачи
            active_task = create_or_get_active_task(db, current_user.id, 'tpsl_analysis')

            if active_task and active_task['status'] == 'running':
                return jsonify({
                    'success': False,
                    'error': 'Анализ уже запущен',
                    'task_id': active_task['task_id'],
                    'progress': active_task['progress_percent']
                })

            # Получаем параметры
            data = request.get_json()
            filters = {
                'score_week_min': data.get('score_week_min', 70),
                'score_month_min': data.get('score_month_min', 70),
                'max_trades_per_15min': data.get('max_trades_per_15min', 3),
                'position_size_usd': data.get('position_size_usd', 100),
                'leverage': data.get('leverage', 5),
                'use_trailing_stop': data.get('use_trailing_stop', False),
                'allowed_hours': data.get('allowed_hours', list(range(24)))
            }

            # Запускаем Celery задачу
            task = analyze_tpsl_optimization.apply_async(args=[current_user.id, filters])

            # Создаем запись в БД
            create_task_record(db, current_user.id, 'tpsl_analysis', task.id)

            return jsonify({
                'success': True,
                'task_id': task.id,
                'message': 'Анализ TP/SL запущен'
            })

        except Exception as e:
            logger.error(f"Error starting TP/SL analysis: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/tpsl_analysis/status')
    @login_required
    def api_tpsl_analysis_status():
        """Получение статуса анализа TP/SL"""
        try:
            active_task = create_or_get_active_task(db, current_user.id, 'tpsl_analysis')

            if not active_task:
                return jsonify({
                    'success': True,
                    'status': 'idle',
                    'message': 'Нет активных задач'
                })

            return jsonify({
                'success': True,
                'task_id': active_task['task_id'],
                'status': active_task['status'],
                'progress': {
                    'current': active_task['progress_current'],
                    'total': active_task['progress_total'],
                    'percent': active_task['progress_percent'],
                    'status': active_task['progress_status']
                },
                'has_result': active_task['status'] == 'completed' and active_task['result_data'] is not None
            })

        except Exception as e:
            logger.error(f"Error getting TP/SL analysis status: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/tpsl_analysis/result')
    @login_required
    def api_tpsl_analysis_result():
        """Получение результата анализа TP/SL"""
        try:
            active_task = create_or_get_active_task(db, current_user.id, 'tpsl_analysis')

            if not active_task:
                return jsonify({
                    'success': False,
                    'error': 'Результаты не найдены'
                })

            if active_task['status'] != 'completed':
                return jsonify({
                    'success': False,
                    'error': 'Анализ еще не завершен',
                    'status': active_task['status']
                })

            # result_data уже dict из JSONB поля БД
            result_data = active_task['result_data']

            if result_data:
                # Возвращаем сами данные, они уже содержат success
                return jsonify(result_data)
            else:
                return jsonify({
                    'success': False,
                    'error': 'Результаты не найдены'
                })

        except Exception as e:
            logger.error(f"Error getting TP/SL analysis result: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    # ===== Trailing Stop Analysis =====
    @app.route('/api/trailing_analysis/start', methods=['POST'])
    @login_required
    def api_trailing_analysis_start():
        """Запуск анализа Trailing Stop"""
        try:
            # Проверяем наличие активной задачи
            active_task = create_or_get_active_task(db, current_user.id, 'trailing_analysis')

            if active_task and active_task['status'] == 'running':
                return jsonify({
                    'success': False,
                    'error': 'Анализ уже запущен',
                    'task_id': active_task['task_id'],
                    'progress': active_task['progress_percent']
                })

            # Получаем параметры
            data = request.get_json()
            filters = {
                'score_week_min': data.get('score_week_min', 70),
                'score_month_min': data.get('score_month_min', 70),
                'max_trades_per_15min': data.get('max_trades_per_15min', 3),
                'take_profit_percent': data.get('take_profit_percent', 4.0),
                'stop_loss_percent': data.get('stop_loss_percent', 3.0),
                'position_size_usd': data.get('position_size_usd', 100),
                'leverage': data.get('leverage', 5),
                'trailing_distance_pct': data.get('trailing_distance_pct', 2.0),
                'trailing_activation_pct': data.get('trailing_activation_pct', 1.0),
                'allowed_hours': data.get('allowed_hours', list(range(24)))
            }

            # Запускаем Celery задачу
            task = analyze_trailing_stop.apply_async(args=[current_user.id, filters])

            # Создаем запись в БД
            create_task_record(db, current_user.id, 'trailing_analysis', task.id)

            return jsonify({
                'success': True,
                'task_id': task.id,
                'message': 'Анализ Trailing Stop запущен'
            })

        except Exception as e:
            logger.error(f"Error starting trailing stop analysis: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/trailing_analysis/status')
    @login_required
    def api_trailing_analysis_status():
        """Получение статуса анализа Trailing Stop"""
        try:
            active_task = create_or_get_active_task(db, current_user.id, 'trailing_analysis')

            if not active_task:
                return jsonify({
                    'success': True,
                    'status': 'idle',
                    'message': 'Нет активных задач'
                })

            return jsonify({
                'success': True,
                'task_id': active_task['task_id'],
                'status': active_task['status'],
                'progress': {
                    'current': active_task['progress_current'],
                    'total': active_task['progress_total'],
                    'percent': active_task['progress_percent'],
                    'status': active_task['progress_status']
                },
                'has_result': active_task['status'] == 'completed' and active_task['result_data'] is not None
            })

        except Exception as e:
            logger.error(f"Error getting trailing stop analysis status: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500

    @app.route('/api/trailing_analysis/result')
    @login_required
    def api_trailing_analysis_result():
        """Получение результата анализа Trailing Stop"""
        try:
            active_task = create_or_get_active_task(db, current_user.id, 'trailing_analysis')

            if not active_task:
                return jsonify({
                    'success': False,
                    'error': 'Результаты не найдены'
                })

            if active_task['status'] != 'completed':
                return jsonify({
                    'success': False,
                    'error': 'Анализ еще не завершен',
                    'status': active_task['status']
                })

            # result_data уже dict из JSONB поля БД
            result_data = active_task['result_data']

            if result_data:
                # Возвращаем сами данные, они уже содержат success
                return jsonify(result_data)
            else:
                return jsonify({
                    'success': False,
                    'error': 'Результаты не найдены'
                })

        except Exception as e:
            logger.error(f"Error getting trailing stop analysis result: {e}")
            return jsonify({'success': False, 'error': str(e)}), 500