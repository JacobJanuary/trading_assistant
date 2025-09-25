"""
Конфигурация Celery для асинхронного выполнения тяжелых задач
"""
from celery import Celery
from config import Config
import logging

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создаем экземпляр Celery
celery_app = Celery(
    'trading_assistant',
    broker=Config.CELERY_BROKER_URL,
    backend=Config.CELERY_RESULT_BACKEND,
    include=['celery_tasks']  # Модуль с задачами
)

# Конфигурация Celery
celery_app.conf.update(
    # Основные настройки
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    
    # Таймауты
    task_time_limit=Config.CELERY_TASK_TIME_LIMIT,
    task_soft_time_limit=Config.CELERY_TASK_SOFT_TIME_LIMIT,
    
    # Настройки результатов
    result_expires=3600,  # Результаты хранятся 1 час
    result_backend_transport_options={
        'master_name': 'mymaster',
        'visibility_timeout': 3600,
        'fanout_prefix': True,
        'fanout_patterns': True
    },
    
    # Оптимизация производительности
    worker_prefetch_multiplier=1,  # Воркер берет только одну задачу за раз
    worker_max_tasks_per_child=100,  # Перезапуск воркера после 100 задач
    
    # Настройки для длительных задач
    task_acks_late=True,  # Подтверждение только после выполнения
    task_reject_on_worker_lost=True,  # Повторить задачу при потере воркера
)

if __name__ == '__main__':
    celery_app.start()