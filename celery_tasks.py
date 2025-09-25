"""
Celery задачи для выполнения тяжелых вычислений
"""
from celery import Task, current_task
from celery_app import celery_app
from database import Database, get_scoring_signals_v2, process_scoring_signals_batch
from datetime import datetime, timedelta
from config import Config
import logging
import json
import uuid
import traceback

logger = logging.getLogger(__name__)

class DatabaseTask(Task):
    """Базовый класс для задач с подключением к БД"""
    _db = None
    
    @property
    def db(self):
        if self._db is None:
            self._db = Database(Config.get_database_url())
            logger.info("Database connection initialized for Celery task")
        return self._db
    
    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """Закрываем соединение после выполнения задачи"""
        if self._db is not None:
            try:
                self._db.close()
                self._db = None
                logger.info(f"Database connection closed for task {task_id}")
            except:
                pass


@celery_app.task(base=DatabaseTask, bind=True, name='analyze_efficiency_combination')
def analyze_efficiency_combination(self, combination_data):
    """
    Анализирует одну комбинацию параметров эффективности
    """
    try:
        # Распаковываем параметры
        score_week_min = combination_data['score_week_min']
        score_month_min = combination_data['score_month_min']
        user_id = combination_data['user_id']
        settings = combination_data['settings']
        days_range = combination_data['days_range']
        combination_id = combination_data['combination_id']
        total_combinations = combination_data['total_combinations']
        
        # Обновляем прогресс
        current_task.update_state(
            state='PROGRESS',
            meta={
                'current': combination_id,
                'total': total_combinations,
                'status': f'Processing combination {combination_id}/{total_combinations}'
            }
        )
        
        # Результаты для этой комбинации
        combination_result = {
            'score_week_min': score_week_min,
            'score_month_min': score_month_min,
            'total_signals': 0,
            'total_pnl': 0.0,
            'tp_count': 0,
            'sl_count': 0,
            'timeout_count': 0,
            'trailing_wins': 0,
            'trailing_losses': 0,
            'days': []
        }
        
        # Обрабатываем каждый день
        for day_idx, date_str in enumerate(days_range):
            # Получаем сигналы для дня
            raw_signals = get_scoring_signals_v2(
                self.db, date_str, 
                score_week_min, score_month_min,
                settings['allowed_hours'],
                settings['max_trades_per_15min']
            )
            
            if raw_signals:
                session_id = f"eff_{user_id}_{uuid.uuid4().hex[:8]}"
                
                # Обрабатываем сигналы
                result = process_scoring_signals_batch(
                    self.db, raw_signals, session_id, user_id,
                    tp_percent=settings['tp_percent'],
                    sl_percent=settings['sl_percent'],
                    position_size=settings['position_size'],
                    leverage=settings['leverage'],
                    use_trailing_stop=settings['use_trailing_stop'],
                    trailing_distance_pct=settings.get('trailing_distance_pct', 2.0),
                    trailing_activation_pct=settings.get('trailing_activation_pct', 1.0)
                )
                
                stats = result['stats']
                daily_pnl = float(stats.get('total_pnl', 0))
                
                # Добавляем к общим результатам
                combination_result['total_signals'] += int(stats.get('total', 0))
                combination_result['total_pnl'] += daily_pnl
                combination_result['tp_count'] += int(stats.get('tp_count', 0))
                combination_result['sl_count'] += int(stats.get('sl_count', 0))
                combination_result['timeout_count'] += int(stats.get('timeout_count', 0))
                
                if settings['use_trailing_stop']:
                    combination_result['trailing_wins'] += int(stats.get('trailing_wins', 0))
                    combination_result['trailing_losses'] += int(stats.get('trailing_losses', 0))
                
                # Сохраняем данные дня
                combination_result['days'].append({
                    'date': date_str,
                    'signal_count': int(stats.get('total', 0)),
                    'daily_pnl': daily_pnl
                })
                
                # Очищаем временные данные
                cleanup_query = """
                    DELETE FROM web.scoring_analysis_results
                    WHERE session_id = %s AND user_id = %s
                """
                self.db.execute_query(cleanup_query, (session_id, user_id))
            
            # Обновляем прогресс внутри комбинации
            progress_percent = int((combination_id / total_combinations) * 100)
            current_task.update_state(
                state='PROGRESS',
                meta={
                    'current': combination_id,
                    'total': total_combinations,
                    'day': day_idx + 1,
                    'total_days': len(days_range),
                    'percent': progress_percent,
                    'status': f'Combination {combination_id}: Day {day_idx + 1}/{len(days_range)}'
                }
            )
        
        return {
            'status': 'success',
            'combination_id': combination_id,
            'result': combination_result
        }
        
    except Exception as e:
        logger.error(f"Error in analyze_efficiency_combination: {e}")
        logger.error(traceback.format_exc())
        return {
            'status': 'error',
            'combination_id': combination_data.get('combination_id', -1),
            'error': str(e)
        }


@celery_app.task(base=DatabaseTask, bind=True, name='analyze_trailing_stop_combination')
def analyze_trailing_stop_combination(self, combination_data):
    """
    Анализирует одну комбинацию параметров Trailing Stop
    """
    try:
        # Распаковываем параметры
        activation_pct = combination_data['activation_pct']
        distance_pct = combination_data['distance_pct']
        stop_loss = combination_data['stop_loss']
        score_week = combination_data['score_week']
        score_month = combination_data['score_month']
        user_id = combination_data['user_id']
        settings = combination_data['settings']
        days_range = combination_data['days_range']
        combination_id = combination_data['combination_id']
        total_combinations = combination_data['total_combinations']
        
        # Обновляем прогресс
        current_task.update_state(
            state='PROGRESS',
            meta={
                'current': combination_id,
                'total': total_combinations,
                'status': f'Processing combination {combination_id}/{total_combinations}'
            }
        )
        
        # Результаты для этой комбинации
        combination_result = {
            'activation': activation_pct,
            'distance': distance_pct,
            'stop_loss': stop_loss,
            'total_signals': 0,
            'total_pnl': 0.0,
            'trailing_count': 0,
            'trailing_wins': 0,
            'trailing_losses': 0,
            'sl_count': 0,
            'tp_count': 0
        }
        
        # Обрабатываем каждый день
        for day_idx, date_str in enumerate(days_range):
            # Получаем сигналы для дня
            raw_signals = get_scoring_signals_v2(
                self.db, date_str,
                score_week, score_month,
                settings['allowed_hours'],
                settings['max_trades_per_15min']
            )
            
            if raw_signals:
                session_id = f"trail_{user_id}_{uuid.uuid4().hex[:8]}"
                
                # Обрабатываем с Trailing Stop
                result = process_scoring_signals_batch(
                    self.db, raw_signals, session_id, user_id,
                    tp_percent=settings['tp_percent'],  # Максимальный TP
                    sl_percent=stop_loss,
                    position_size=settings['position_size'],
                    leverage=settings['leverage'],
                    use_trailing_stop=True,
                    trailing_activation_pct=activation_pct,
                    trailing_distance_pct=distance_pct
                )
                
                stats = result['stats']
                
                # Добавляем к общим результатам
                combination_result['total_signals'] += int(stats.get('total', 0))
                combination_result['total_pnl'] += float(stats.get('total_pnl', 0))
                combination_result['trailing_count'] += int(stats.get('trailing_count', 0))
                combination_result['trailing_wins'] += int(stats.get('trailing_wins', 0))
                combination_result['trailing_losses'] += int(stats.get('trailing_losses', 0))
                combination_result['sl_count'] += int(stats.get('sl_count', 0))
                combination_result['tp_count'] += int(stats.get('tp_count', 0))
                
                # Очищаем временные данные
                cleanup_query = """
                    DELETE FROM web.scoring_analysis_results
                    WHERE session_id = %s AND user_id = %s
                """
                self.db.execute_query(cleanup_query, (session_id, user_id))
            
            # Обновляем прогресс
            progress_percent = int((combination_id / total_combinations) * 100)
            current_task.update_state(
                state='PROGRESS',
                meta={
                    'current': combination_id,
                    'total': total_combinations,
                    'day': day_idx + 1,
                    'total_days': len(days_range),
                    'percent': progress_percent,
                    'status': f'Trailing {combination_id}: Day {day_idx + 1}/{len(days_range)}'
                }
            )
        
        return {
            'status': 'success',
            'combination_id': combination_id,
            'result': combination_result
        }
        
    except Exception as e:
        logger.error(f"Error in analyze_trailing_stop_combination: {e}")
        logger.error(traceback.format_exc())
        return {
            'status': 'error',
            'combination_id': combination_data.get('combination_id', -1),
            'error': str(e)
        }


@celery_app.task(base=DatabaseTask, bind=True, name='analyze_tp_sl_combination')
def analyze_tp_sl_combination(self, combination_data):
    """
    Анализирует одну комбинацию параметров TP/SL
    """
    try:
        # Распаковываем параметры
        tp_percent = combination_data['tp_percent']
        sl_percent = combination_data['sl_percent']
        score_week = combination_data['score_week']
        score_month = combination_data['score_month']
        user_id = combination_data['user_id']
        settings = combination_data['settings']
        days_range = combination_data['days_range']
        combination_id = combination_data['combination_id']
        total_combinations = combination_data['total_combinations']
        
        # Обновляем прогресс
        current_task.update_state(
            state='PROGRESS',
            meta={
                'current': combination_id,
                'total': total_combinations,
                'status': f'Processing TP/SL {combination_id}/{total_combinations}'
            }
        )
        
        # Результаты для этой комбинации
        combination_result = {
            'tp': tp_percent,
            'sl': sl_percent,
            'total_signals': 0,
            'total_pnl': 0.0,
            'tp_count': 0,
            'sl_count': 0,
            'timeout_count': 0
        }
        
        # Обрабатываем каждый день
        for day_idx, date_str in enumerate(days_range):
            # Получаем сигналы для дня
            raw_signals = get_scoring_signals_v2(
                self.db, date_str,
                score_week, score_month,
                settings['allowed_hours'],
                settings['max_trades_per_15min']
            )
            
            if raw_signals:
                session_id = f"tpsl_{user_id}_{uuid.uuid4().hex[:8]}"
                
                # Обрабатываем с текущими TP/SL
                result = process_scoring_signals_batch(
                    self.db, raw_signals, session_id, user_id,
                    tp_percent=tp_percent,
                    sl_percent=sl_percent,
                    position_size=settings['position_size'],
                    leverage=settings['leverage'],
                    use_trailing_stop=False  # Для анализа TP/SL используем Fixed режим
                )
                
                stats = result['stats']
                
                # Добавляем к общим результатам
                combination_result['total_signals'] += int(stats.get('total', 0))
                combination_result['total_pnl'] += float(stats.get('total_pnl', 0))
                combination_result['tp_count'] += int(stats.get('tp_count', 0))
                combination_result['sl_count'] += int(stats.get('sl_count', 0))
                combination_result['timeout_count'] += int(stats.get('timeout_count', 0))
                
                # Очищаем временные данные
                cleanup_query = """
                    DELETE FROM web.scoring_analysis_results
                    WHERE session_id = %s AND user_id = %s
                """
                self.db.execute_query(cleanup_query, (session_id, user_id))
            
            # Обновляем прогресс
            progress_percent = int((combination_id / total_combinations) * 100)
            current_task.update_state(
                state='PROGRESS',
                meta={
                    'current': combination_id,
                    'total': total_combinations,
                    'day': day_idx + 1,
                    'total_days': len(days_range),
                    'percent': progress_percent,
                    'status': f'TP/SL {combination_id}: Day {day_idx + 1}/{len(days_range)}'
                }
            )
        
        return {
            'status': 'success',
            'combination_id': combination_id,
            'result': combination_result
        }
        
    except Exception as e:
        logger.error(f"Error in analyze_tp_sl_combination: {e}")
        logger.error(traceback.format_exc())
        return {
            'status': 'error',
            'combination_id': combination_data.get('combination_id', -1),
            'error': str(e)
        }