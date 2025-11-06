"""
Конфигурация Gunicorn для production
"""
import multiprocessing
import os
import time

# Основные настройки
bind = "unix:/home/elcrypto/trading_assistant/trading_assistant.sock"
# Уменьшаем количество воркеров для стабильности SSL соединений
workers = min(multiprocessing.cpu_count() + 1, 4)  # Максимум 4 воркера

# КРИТИЧНО: Увеличиваем таймаут для длительных операций анализа
timeout = 300  # 5 минут вместо стандартных 30 секунд

# Увеличиваем лимит памяти (если используется с systemd)
# Это нужно настроить в systemd service файле

# Тип воркеров - для SSE лучше использовать gevent или eventlet
worker_class = 'sync'  # Можно изменить на 'gevent' для лучшей производительности SSE

# keepalive поддерживается только в новых версиях
# Раскомментируйте если у вас Gunicorn 19.2+
# keepalive = 75

# Логирование
accesslog = '/home/elcrypto/trading_assistant/gunicorn_access.log'
errorlog = '/home/elcrypto/trading_assistant/gunicorn_error.log'
loglevel = 'info'

# Настройки для обработки больших запросов
max_requests = 1000  # Перезапуск воркера после 1000 запросов
max_requests_jitter = 50  # Случайный разброс для предотвращения одновременного перезапуска

# Graceful timeout для завершения длительных операций
graceful_timeout = 120

# Дополнительные настройки для стабильности (для новых версий)
# worker_connections = 1000  # Максимум соединений на воркер
# threads = 2  # Количество потоков на воркер для лучшей производительности

# КРИТИЧНО: ОТКЛЮЧАЕМ preload_app для правильной работы connection pool
# При preload_app=True пул соединений создается в мастер-процессе,
# а затем форкается в воркеры, что приводит к проблемам с соединениями
preload_app = False

# Настройки для работы с прокси
forwarded_allow_ips = '*'
secure_scheme_headers = {'X-Forwarded-Proto': 'https'}

def when_ready(server):
    server.log.info("Server is ready. Spawning workers")

def worker_int(worker):
    worker.log.info("Worker received INT or QUIT signal")

def pre_fork(server, worker):
    """Вызывается перед форком каждого воркера"""
    server.log.info("Worker spawned (pid: %s)", worker.pid)
    # Небольшая задержка между запуском воркеров для предотвращения
    # одновременного создания множества SSL соединений
    time.sleep(0.5)

def pre_exec(server):
    server.log.info("Forked child, re-executing.")

def on_exit(server):
    server.log.info("Shutting down gracefully")