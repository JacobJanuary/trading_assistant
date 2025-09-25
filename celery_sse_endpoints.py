"""
Новые SSE эндпоинты для работы с Celery задачами
"""
from flask import Response, request, jsonify
from celery.result import AsyncResult
from celery_app import celery_app
from celery_tasks import (
    analyze_efficiency_combination,
    analyze_trailing_stop_combination, 
    analyze_tp_sl_combination
)
from datetime import datetime, timedelta
from config import Config
import json
import time
import logging

logger = logging.getLogger(__name__)


def analyze_efficiency_celery(user_id):
    """SSE endpoint для анализа эффективности через Celery"""
    try:
        # Получаем параметры
        score_week_min_param = int(request.args.get('score_week_min', 60))
        score_week_max_param = int(request.args.get('score_week_max', 80))
        score_month_min_param = int(request.args.get('score_month_min', 60))
        score_month_max_param = int(request.args.get('score_month_max', 80))
        step_param = int(request.args.get('step', 10))
        max_trades_per_15min = int(request.args.get('max_trades_per_15min', 3))
        force_recalc = request.args.get('force_recalc', 'false').lower() == 'true'
        
        # user_id теперь передается как параметр
        logger.info(f"Starting Celery efficiency analysis for user {user_id}")
    except Exception as e:
        logger.error(f"Error in analyze_efficiency_celery initialization: {e}", exc_info=True)
        return Response(f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n", 
                       mimetype='text/event-stream')
    
    def generate():
        try:
            # Получаем настройки пользователя из БД
            from database import Database
            db = Database(Config.get_database_url())
            
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
            
            # Подготавливаем параметры
            settings_dict = {
                'use_trailing_stop': settings.get('use_trailing_stop', False),
                'trailing_distance_pct': float(settings.get('trailing_distance_pct', 2.0)),
                'trailing_activation_pct': float(settings.get('trailing_activation_pct', 1.0)),
                'tp_percent': float(settings.get('take_profit_percent', 4.0)),
                'sl_percent': float(settings.get('stop_loss_percent', 3.0)),
                'position_size': float(settings.get('position_size_usd', 100.0)),
                'leverage': int(settings.get('leverage', 5)),
                'allowed_hours': settings.get('allowed_hours', list(range(24))),
                'max_trades_per_15min': max_trades_per_15min
            }
            
            # Определяем период анализа (исключаем последние 2 дня)
            end_date = datetime.now().date() - timedelta(days=2)
            start_date = end_date - timedelta(days=29)
            
            # Формируем список дней
            days_range = []
            current_date = start_date
            while current_date <= end_date:
                days_range.append(current_date.strftime('%Y-%m-%d'))
                current_date += timedelta(days=1)
            
            # Генерируем комбинации
            week_steps = list(range(score_week_min_param, score_week_max_param + 1, step_param))
            month_steps = list(range(score_month_min_param, score_month_max_param + 1, step_param))
            
            total_combinations = len(week_steps) * len(month_steps)
            
            yield f"data: {json.dumps({'type': 'start', 'total_combinations': total_combinations, 'days': len(days_range)})}\n\n"
            
            # Запускаем Celery задачи для каждой комбинации
            tasks = []
            combination_id = 0
            
            for score_week_min in week_steps:
                for score_month_min in month_steps:
                    combination_id += 1
                    
                    combination_data = {
                        'combination_id': combination_id,
                        'total_combinations': total_combinations,
                        'score_week_min': score_week_min,
                        'score_month_min': score_month_min,
                        'user_id': user_id,
                        'settings': settings_dict,
                        'days_range': days_range
                    }
                    
                    # Запускаем задачу асинхронно
                    task = analyze_efficiency_combination.delay(combination_data)
                    tasks.append({
                        'task_id': task.id,
                        'combination_id': combination_id,
                        'score_week_min': score_week_min,
                        'score_month_min': score_month_min
                    })
            
            yield f"data: {json.dumps({'type': 'tasks_started', 'count': len(tasks)})}\n\n"
            
            # Отслеживаем прогресс выполнения
            completed_tasks = []
            all_results = []
            last_update = time.time()
            
            while len(completed_tasks) < len(tasks):
                current_time = time.time()
                
                # Отправляем heartbeat каждые 5 секунд
                if current_time - last_update > 5:
                    yield f": heartbeat\n\n"
                    last_update = current_time
                
                for task_info in tasks:
                    if task_info['task_id'] in completed_tasks:
                        continue
                    
                    result = AsyncResult(task_info['task_id'], app=celery_app)
                    
                    if result.state == 'PENDING':
                        continue
                    elif result.state == 'PROGRESS':
                        # Отправляем обновление прогресса
                        meta = result.info
                        yield f"data: {json.dumps({
                            'type': 'progress',
                            'combination_id': task_info['combination_id'],
                            'percent': meta.get('percent', 0),
                            'status': meta.get('status', ''),
                            'current': meta.get('current', 0),
                            'total': meta.get('total', total_combinations)
                        })}\n\n"
                    elif result.state == 'SUCCESS':
                        # Задача выполнена успешно
                        task_result = result.get()
                        if task_result['status'] == 'success':
                            all_results.append(task_result['result'])
                            yield f"data: {json.dumps({
                                'type': 'combination_complete',
                                'combination_id': task_info['combination_id'],
                                'result': task_result['result']
                            })}\n\n"
                        else:
                            logger.error(f"Task {task_info['task_id']} failed: {task_result.get('error')}")
                        
                        completed_tasks.append(task_info['task_id'])
                    elif result.state == 'FAILURE':
                        # Задача завершилась с ошибкой
                        logger.error(f"Task {task_info['task_id']} failed")
                        completed_tasks.append(task_info['task_id'])
                        yield f"data: {json.dumps({
                            'type': 'error',
                            'combination_id': task_info['combination_id'],
                            'message': 'Ошибка выполнения задачи'
                        })}\n\n"
                
                time.sleep(0.5)  # Небольшая пауза между проверками
            
            # Находим лучшую комбинацию
            if all_results:
                best_result = max(all_results, key=lambda x: x['total_pnl'])
                
                yield f"data: {json.dumps({
                    'type': 'complete',
                    'results': all_results,
                    'best_result': best_result,
                    'total_combinations': total_combinations
                })}\n\n"
            else:
                yield f"data: {json.dumps({
                    'type': 'complete',
                    'results': [],
                    'message': 'Нет результатов'
                })}\n\n"
            
        except Exception as e:
            logger.error(f"Error in efficiency analysis: {e}", exc_info=True)
            error_msg = f"Ошибка анализа: {str(e)}"
            yield f"data: {json.dumps({'type': 'error', 'message': error_msg})}\n\n"
        finally:
            if 'db' in locals():
                db.close()
    
    return Response(generate(), mimetype="text/event-stream")


def analyze_trailing_stop_celery(user_id):
    """SSE endpoint для анализа Trailing Stop через Celery"""
    # Получаем параметры
    score_week = int(request.args.get('score_week', 70))
    score_month = int(request.args.get('score_month', 70))
    activation_min = float(request.args.get('activation_min', 0.5))
    activation_max = float(request.args.get('activation_max', 3.0))
    activation_step = float(request.args.get('activation_step', 0.5))
    distance_min = float(request.args.get('distance_min', 0.5))
    distance_max = float(request.args.get('distance_max', 3.0))
    distance_step = float(request.args.get('distance_step', 0.5))
    stop_loss_min = float(request.args.get('stop_loss_min', 1.0))
    stop_loss_max = float(request.args.get('stop_loss_max', 5.0))
    stop_loss_step = float(request.args.get('stop_loss_step', 1.0))
    max_trades_per_15min = int(request.args.get('max_trades_per_15min', 3))
    
    # user_id теперь передается как параметр
    
    def generate():
        try:
            # Получаем настройки пользователя
            from database import Database
            db = Database(Config.get_database_url())
            
            settings_query = """
                SELECT position_size_usd, leverage, allowed_hours, take_profit_percent
                FROM web.user_signal_filters
                WHERE user_id = %s
            """
            user_settings = db.execute_query(settings_query, (user_id,), fetch=True)
            
            if not user_settings:
                yield f"data: {json.dumps({'type': 'error', 'message': 'Настройки пользователя не найдены'})}\n\n"
                return
            
            settings = user_settings[0]
            settings_dict = {
                'position_size': float(settings.get('position_size_usd', 100.0)),
                'leverage': int(settings.get('leverage', 5)),
                'allowed_hours': settings.get('allowed_hours', list(range(24))),
                'tp_percent': float(settings.get('take_profit_percent', 10.0)),
                'max_trades_per_15min': max_trades_per_15min
            }
            
            # Определяем период анализа
            end_date = datetime.now().date() - timedelta(days=2)
            start_date = end_date - timedelta(days=29)
            
            days_range = []
            current_date = start_date
            while current_date <= end_date:
                days_range.append(current_date.strftime('%Y-%m-%d'))
                current_date += timedelta(days=1)
            
            # Генерируем комбинации
            import numpy as np
            activation_values = np.arange(activation_min, activation_max + activation_step, activation_step).tolist()
            distance_values = np.arange(distance_min, distance_max + distance_step, distance_step).tolist()
            stop_loss_values = np.arange(stop_loss_min, stop_loss_max + stop_loss_step, stop_loss_step).tolist()
            
            total_combinations = len(activation_values) * len(distance_values) * len(stop_loss_values)
            
            yield f"data: {json.dumps({'type': 'start', 'total_combinations': total_combinations})}\n\n"
            
            # Запускаем задачи
            tasks = []
            combination_id = 0
            
            for activation_pct in activation_values:
                for distance_pct in distance_values:
                    for stop_loss in stop_loss_values:
                        combination_id += 1
                        
                        combination_data = {
                            'combination_id': combination_id,
                            'total_combinations': total_combinations,
                            'activation_pct': activation_pct,
                            'distance_pct': distance_pct,
                            'stop_loss': stop_loss,
                            'score_week': score_week,
                            'score_month': score_month,
                            'user_id': user_id,
                            'settings': settings_dict,
                            'days_range': days_range
                        }
                        
                        task = analyze_trailing_stop_combination.delay(combination_data)
                        tasks.append({
                            'task_id': task.id,
                            'combination_id': combination_id
                        })
            
            yield f"data: {json.dumps({'type': 'tasks_started', 'count': len(tasks)})}\n\n"
            
            # Отслеживаем прогресс
            completed_tasks = []
            all_results = []
            last_update = time.time()
            
            while len(completed_tasks) < len(tasks):
                current_time = time.time()
                
                if current_time - last_update > 5:
                    yield f": heartbeat\n\n"
                    last_update = current_time
                
                for task_info in tasks:
                    if task_info['task_id'] in completed_tasks:
                        continue
                    
                    result = AsyncResult(task_info['task_id'], app=celery_app)
                    
                    if result.state == 'PROGRESS':
                        meta = result.info
                        yield f"data: {json.dumps({
                            'type': 'progress',
                            'combination_id': task_info['combination_id'],
                            'percent': meta.get('percent', 0),
                            'status': meta.get('status', '')
                        })}\n\n"
                    elif result.state == 'SUCCESS':
                        task_result = result.get()
                        if task_result['status'] == 'success':
                            all_results.append(task_result['result'])
                            yield f"data: {json.dumps({
                                'type': 'combination_complete',
                                'combination_id': task_info['combination_id'],
                                'result': task_result['result']
                            })}\n\n"
                        completed_tasks.append(task_info['task_id'])
                    elif result.state == 'FAILURE':
                        completed_tasks.append(task_info['task_id'])
                
                time.sleep(0.5)
            
            # Находим лучшую комбинацию
            if all_results:
                best_result = max(all_results, key=lambda x: x['total_pnl'])
                yield f"data: {json.dumps({
                    'type': 'complete',
                    'results': all_results,
                    'best_result': best_result
                })}\n\n"
            
        except Exception as e:
            logger.error(f"Error in trailing stop analysis: {e}")
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        finally:
            if 'db' in locals():
                db.close()
    
    return Response(generate(), mimetype="text/event-stream")