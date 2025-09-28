"""
Параллельная версия efficiency_analysis БЕЗ deadlock
Использует группы для параллельной обработки без chord
"""
from celery import Celery, group
from config import Config
from database import Database, get_scoring_signals_v2, process_scoring_signals_batch
import psycopg
from psycopg.rows import dict_row
import logging
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
import uuid

logger = logging.getLogger(__name__)

# Создаем Celery приложение
celery = Celery(
    'trading_assistant',
    broker=Config.CELERY_BROKER_URL,
    backend=Config.CELERY_RESULT_BACKEND
)

# Настройки для параллельной работы
celery.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=7200,  # 120 минут на главную задачу (для 700+ комбинаций)
    task_soft_time_limit=7000,  # 116 минут soft limit
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)

@celery.task(name='analyze_single_combination')
def analyze_single_combination(combo_data):
    """Анализ одной комбинации параметров"""
    try:
        week_min = combo_data['week']
        month_min = combo_data['month']
        filters = combo_data['filters']
        date_range = combo_data['date_range']
        user_id = combo_data['user_id']

        logger.info(f"Analyzing combination {week_min}/{month_min}")

        result = {
            'score_week': week_min,
            'score_month': month_min,
            'total_pnl': 0,
            'total_signals': 0,
            'tp_count': 0,
            'sl_count': 0,
            'win_rate': 0
        }

        # Парсим даты
        start = datetime.fromisoformat(date_range['start'])
        end = datetime.fromisoformat(date_range['end'])

        db = Database(database_url=Config.get_database_url())

        # Анализируем каждый день периода
        days = (end - start).days + 1
        for day_offset in range(days):
            date = end - timedelta(days=day_offset)
            date_str = date.strftime('%Y-%m-%d')

            # Получаем сигналы
            signals = get_scoring_signals_v2(
                db,
                date_filter=date_str,
                score_week_min=week_min,
                score_month_min=month_min,
                allowed_hours=filters.get('allowed_hours', list(range(24))),
                max_trades_per_15min=filters.get('max_trades_per_15min', 3)
            )

            if signals:
                # Обрабатываем сигналы
                batch_result = process_scoring_signals_batch(
                    db,
                    signals,
                    session_id=f"eff_{week_min}_{month_min}_{date_str}",
                    user_id=user_id,
                    tp_percent=filters.get('take_profit_percent', 4.0),
                    sl_percent=filters.get('stop_loss_percent', 3.0),
                    position_size=filters.get('position_size_usd', 100)
                )

                if batch_result and 'stats' in batch_result:
                    stats = batch_result['stats']
                    result['total_signals'] += len(signals)
                    result['total_pnl'] += float(stats.get('total_pnl', 0))
                    result['tp_count'] += stats.get('tp_count', 0)
                    result['sl_count'] += stats.get('sl_count', 0)

        # Вычисляем win rate
        total_trades = result['tp_count'] + result['sl_count']
        if total_trades > 0:
            result['win_rate'] = round(result['tp_count'] / total_trades * 100, 2)

        logger.info(f"Combination {week_min}/{month_min} completed: {result['total_signals']} signals, PnL: {result['total_pnl']}")
        return result

    except Exception as e:
        logger.error(f"Error in analyze_single_combination: {e}")
        return None

@celery.task(bind=True, name='analyze_efficiency_parallel')
def analyze_efficiency_parallel(self, user_id, filters):
    """
    Главная задача с ПРАВИЛЬНОЙ параллельной обработкой
    БЕЗ chord и deadlock
    """
    task_id = self.request.id or str(uuid.uuid4())
    logger.info(f"Starting parallel analysis task {task_id} for user {user_id}")

    try:
        db_url = Config.get_database_url()

        # Обновляем существующую запись (созданную в API)
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE web.analysis_tasks
                    SET status = 'running',
                        progress_status = 'Запускаем параллельную обработку...',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s
                """, (task_id,))
                conn.commit()

        # Генерируем комбинации
        week_min = filters.get('score_week_min', 60)
        week_max = filters.get('score_week_max', 80)
        month_min = filters.get('score_month_min', 60)
        month_max = filters.get('score_month_max', 80)
        step = filters.get('step', 10)

        combinations = []
        for week in range(week_min, week_max + 1, step):
            for month in range(month_min, month_max + 1, step):
                combinations.append({'week': week, 'month': month})

        total_combinations = len(combinations)
        logger.info(f"Starting parallel analysis of {total_combinations} combinations")

        # Период анализа
        end_date = datetime.now()
        start_date = end_date - timedelta(days=30)
        date_range = {
            'start': start_date.isoformat(),
            'end': end_date.isoformat()
        }

        # Создаем подзадачи
        subtasks = []
        for combo in combinations:
            subtasks.append(analyze_single_combination.s({
                'week': combo['week'],
                'month': combo['month'],
                'filters': filters,
                'date_range': date_range,
                'user_id': user_id
            }))

        # Запускаем группу БЕЗ chord callback
        job = group(subtasks).apply_async()

        # Обновляем статус
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                status_msg = f'Обрабатываем {total_combinations} комбинаций параллельно...'
                cur.execute("""
                    UPDATE web.analysis_tasks
                    SET progress_total = %s,
                        progress_status = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s
                """, (total_combinations, status_msg, task_id))
                conn.commit()

        # Собираем результаты БЕЗ блокировки
        results = []
        completed = 0
        failed = 0
        # Динамический таймаут: 30 секунд на комбинацию, минимум 10 минут
        max_wait_time = max(600, total_combinations * 30)
        start_time = time.time()
        last_progress_time = time.time()

        logger.info(f"Collecting results for {total_combinations} combinations, timeout: {max_wait_time}s")

        while completed + failed < total_combinations:
            # Проверяем таймаут
            elapsed = time.time() - start_time
            if elapsed > max_wait_time:
                logger.warning(f"Timeout reached after {elapsed:.1f}s, collected {completed}/{total_combinations} results")
                break

            # Проверяем застой (если нет прогресса более 2 минут)
            if time.time() - last_progress_time > 120:
                logger.warning(f"No progress for 2 minutes, collected {completed}/{total_combinations} results")
                break

            # Проверяем готовые результаты
            progress_made = False

            # Ждем инициализации результатов (максимум 10 секунд при первой итерации)
            if completed == 0 and failed == 0:
                init_timeout = 10
                init_start = time.time()
                while time.time() - init_start < init_timeout:
                    if hasattr(job, 'results') and job.results:
                        logger.info(f"job.results initialized after {time.time() - init_start:.1f}s")
                        break
                    time.sleep(0.5)
                    logger.debug(f"Waiting for job.results initialization...")

                if not hasattr(job, 'results') or not job.results:
                    logger.error(f"job.results not initialized after {init_timeout}s, trying alternative method")
                    # Пропускаем эту итерацию, попробуем снова
                    time.sleep(2)
                    continue

            # Теперь безопасно итерировать
            for i, result in enumerate(job.results):
                if result.ready():
                    if not result.failed():
                        try:
                            # Получаем результат без блокировки
                            data = result.result
                            if data and i < len(combinations) and data not in results:
                                results.append(data)
                                completed += 1
                                progress_made = True
                                last_progress_time = time.time()

                                # Обновляем прогресс каждые 10 результатов или при достижении вех
                                if completed % 10 == 0 or completed == total_combinations:
                                    percent = round((completed / total_combinations) * 100)
                                    status_text = f'Обработано {completed} из {total_combinations} комбинаций'
                                    with psycopg.connect(db_url) as conn:
                                        with conn.cursor() as cur:
                                            cur.execute("""
                                                UPDATE web.analysis_tasks
                                                SET progress_current = %s,
                                                    progress_percent = %s,
                                                    progress_status = %s,
                                                    updated_at = CURRENT_TIMESTAMP
                                                WHERE task_id = %s
                                            """, (completed, percent, status_text, task_id))
                                            conn.commit()
                        except Exception as e:
                            logger.error(f"Error getting result {i}: {e}")
                            failed += 1
                            progress_made = True
                    else:
                        # Задача провалилась
                        if i not in [r.get('index', -1) for r in results] and failed < total_combinations:
                            failed += 1
                            progress_made = True
                            logger.warning(f"Task {i} failed")

            # Небольшая пауза между проверками
            if completed < total_combinations:
                time.sleep(1)

        # Обрабатываем результаты
        if results:
            # Фильтруем None результаты
            valid_results = [r for r in results if r is not None]

            # Сортируем по PnL
            sorted_results = sorted(valid_results, key=lambda x: x['total_pnl'], reverse=True)
            best = sorted_results[0] if sorted_results else None
        else:
            sorted_results = []
            best = None

        # Формируем финальный результат
        result_data = {
            'success': True,
            'results': sorted_results[:50],  # Топ-50
            'best_result': best,
            'total_combinations': total_combinations,
            'valid_combinations': len(sorted_results),
            'analysis_date': datetime.now().isoformat()
        }

        # Сохраняем результат
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE web.analysis_tasks
                    SET status = 'completed',
                        result_data = %s::jsonb,
                        progress_status = 'Анализ завершен',
                        progress_percent = 100,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s
                """, (json.dumps(result_data), task_id))
                conn.commit()

        logger.info(f"Task {task_id} completed with {len(sorted_results)} valid results")
        return result_data

    except Exception as e:
        logger.error(f"Error in analyze_efficiency_parallel: {e}")
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE web.analysis_tasks
                    SET status = 'failed',
                        error_message = %s,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s
                """, (str(e), task_id))
                conn.commit()
        raise

# Регистрируем с алиасами для совместимости
analyze_efficiency_30days = celery.task(name='analyze_efficiency_30days')(analyze_efficiency_parallel)

# Экспортируем app
app = celery

# Функция для получения статуса
def get_task_status_from_db(task_id):
    """Получает статус задачи из БД"""
    try:
        db_url = Config.get_database_url()
        with psycopg.connect(db_url) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("""
                    SELECT status, progress_current, progress_total,
                           progress_percent, progress_status, error_message
                    FROM web.analysis_tasks
                    WHERE task_id = %s
                """, (task_id,))
                result = cur.fetchone()
                if result:
                    return {
                        'state': 'SUCCESS' if result['status'] == 'completed' else 'PROGRESS',
                        'current': result['progress_current'],
                        'total': result['progress_total'],
                        'percent': result['progress_percent'],
                        'status': result['progress_status'],
                        'error': result['error_message']
                    }
    except Exception as e:
        logger.error(f"Error getting task status: {e}")

    return {'state': 'PENDING', 'current': 0, 'total': 0, 'percent': 0, 'status': 'Неизвестно'}