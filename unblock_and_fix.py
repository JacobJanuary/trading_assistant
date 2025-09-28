#!/usr/bin/env python3
"""
Скрипт для разблокировки зависших задач и применения исправлений
"""
import psycopg
from config import Config
import redis
import sys
import time

def unblock_stuck_tasks():
    """Разблокирует все зависшие задачи efficiency_analysis"""
    conn_str = Config.get_database_url()

    print("🔓 РАЗБЛОКИРОВКА ЗАВИСШИХ ЗАДАЧ")
    print("=" * 60)

    with psycopg.connect(conn_str) as conn:
        with conn.cursor() as cur:
            # 1. Находим все зависшие задачи
            cur.execute("""
                SELECT
                    task_id,
                    task_type,
                    progress_status,
                    EXTRACT(EPOCH FROM (NOW() - updated_at)) as seconds_ago
                FROM web.analysis_tasks
                WHERE status = 'running'
                    AND (
                        updated_at < NOW() - INTERVAL '5 minutes'
                        OR progress_status LIKE '%параллельно%'
                    )
            """)

            stuck_tasks = cur.fetchall()

            if not stuck_tasks:
                print("✅ Зависших задач не обнаружено")
                return []

            print(f"Найдено {len(stuck_tasks)} зависших задач:\n")

            task_ids = []
            for task in stuck_tasks:
                print(f"Task ID: {task[0]}")
                print(f"  Type: {task[1]}")
                print(f"  Status: {task[2]}")
                print(f"  Застряла {int(task[3])} секунд назад")
                task_ids.append(task[0])

            # 2. Помечаем задачи как failed с объяснением
            print("\n🔧 Помечаем задачи как проваленные...")

            for task_id in task_ids:
                cur.execute("""
                    UPDATE web.analysis_tasks
                    SET status = 'failed',
                        error_message = 'Задача заблокирована из-за проблемы с chord в Celery. Требуется перезапуск с новым алгоритмом.',
                        updated_at = CURRENT_TIMESTAMP
                    WHERE task_id = %s
                """, (task_id,))

            conn.commit()
            print(f"✅ Разблокировано {len(task_ids)} задач")

            return task_ids

def clear_celery_queue():
    """Очищает очередь Celery от зависших задач"""
    print("\n🧹 ОЧИСТКА ОЧЕРЕДИ CELERY")
    print("=" * 60)

    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=False)

        # Очищаем метаданные задач
        keys = r.keys('celery-task-meta-*')
        if keys:
            deleted = r.delete(*keys)
            print(f"✅ Удалено {deleted} метаданных задач")

        # Очищаем очередь
        queue_len = r.llen('celery')
        if queue_len > 0:
            r.delete('celery')
            print(f"✅ Очищена очередь ({queue_len} задач)")

        # Очищаем unacked
        unacked = r.keys('unacked*')
        if unacked:
            r.delete(*unacked)
            print(f"✅ Очищено {len(unacked)} неподтвержденных задач")

    except Exception as e:
        print(f"⚠️ Ошибка при очистке Redis: {e}")

def restart_celery_with_fix():
    """Перезапускает Celery с исправленным модулем"""
    print("\n🔄 ПЕРЕЗАПУСК CELERY С ИСПРАВЛЕНИЯМИ")
    print("=" * 60)

    import subprocess

    # Останавливаем текущие воркеры
    print("1. Останавливаем текущие воркеры...")
    subprocess.run(['pkill', '-f', 'celery.*worker'], check=False)
    time.sleep(3)

    # Принудительная остановка если нужно
    subprocess.run(['pkill', '-9', '-f', 'celery.*worker'], check=False)
    time.sleep(1)

    # Запускаем с новым модулем
    print("2. Запускаем Celery с исправленным модулем...")

    cmd = [
        'celery', '-A', 'celery_efficiency_fixed', 'worker',
        '--loglevel=info',
        '--concurrency=6',  # Уменьшено с 8
        '--pool=prefork',
        '--max-tasks-per-child=10',
        '--time-limit=300',
        '--soft-time-limit=240',
        '--without-gossip',
        '--without-mingle',
        '--without-heartbeat',
        '-n', 'worker_fixed@%h',
        '--pidfile=/home/elcrypto/trading_assistant/celery_fixed.pid',
        '--logfile=/home/elcrypto/trading_assistant/logs/celery_fixed.log',
        '--detach'
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd='/home/elcrypto/trading_assistant')

    if result.returncode == 0:
        print("✅ Celery успешно запущен с исправлениями")
        print("\n📝 Новые настройки:")
        print("  - Модуль: celery_efficiency_fixed")
        print("  - Алгоритм: группы вместо chord")
        print("  - Воркеров: 6")
        print("  - Таймаут: 5 минут на задачу")
        print("  - Мониторинг: активный с автовосстановлением")
    else:
        print(f"❌ Ошибка запуска: {result.stderr}")
        return False

    return True

def test_new_task():
    """Тестовый запуск новой задачи"""
    print("\n🧪 ТЕСТ НОВОЙ СИСТЕМЫ")
    print("=" * 60)

    try:
        from celery_efficiency_fixed import analyze_efficiency_no_deadlock

        # Тестовые параметры (малый объем)
        test_filters = {
            'score_week_min': 70,
            'score_week_max': 80,
            'score_month_min': 70,
            'score_month_max': 80,
            'step': 10,
            'allowed_hours': list(range(24)),
            'max_trades_per_15min': 3,
            'take_profit_percent': 4.0,
            'stop_loss_percent': 3.0,
            'position_size_usd': 100
        }

        print("Запускаем тестовую задачу (4 комбинации)...")
        result = analyze_efficiency_no_deadlock.delay(1, test_filters)
        print(f"✅ Задача запущена: {result.id}")

        # Ждем немного
        print("\nОжидание 10 секунд...")
        time.sleep(10)

        # Проверяем статус
        conn_str = Config.get_database_url()
        with psycopg.connect(conn_str) as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT status, progress_status, progress_percent
                    FROM web.analysis_tasks
                    WHERE task_id = %s
                """, (result.id,))

                status = cur.fetchone()
                if status:
                    print(f"\nСтатус задачи:")
                    print(f"  Status: {status[0]}")
                    print(f"  Progress: {status[1]}")
                    print(f"  Percent: {status[2]}%")

                    if status[0] == 'running' and status[2] > 0:
                        print("✅ Система работает корректно!")
                        return True
                else:
                    print("⚠️ Задача не найдена в БД")

    except Exception as e:
        print(f"❌ Ошибка теста: {e}")

    return False

def main():
    """Главная функция"""
    print("=" * 60)
    print("🛠️  ИСПРАВЛЕНИЕ БЛОКИРОВКИ EFFICIENCY_ANALYSIS")
    print("=" * 60)

    # 1. Разблокируем зависшие задачи
    blocked_tasks = unblock_stuck_tasks()

    # 2. Очищаем очередь
    clear_celery_queue()

    # 3. Перезапускаем Celery
    if not restart_celery_with_fix():
        print("\n❌ Не удалось перезапустить Celery")
        return 1

    # 4. Тестируем новую систему
    time.sleep(5)
    if test_new_task():
        print("\n" + "=" * 60)
        print("✅ СИСТЕМА УСПЕШНО ИСПРАВЛЕНА!")
        print("=" * 60)
        print("\n📌 Рекомендации:")
        print("1. Мониторинг: tail -f /home/elcrypto/trading_assistant/logs/celery_fixed.log")
        print("2. Статус: ps aux | grep celery")
        print("3. БД: SELECT * FROM web.analysis_tasks WHERE status='running';")

        if blocked_tasks:
            print(f"\n⚠️ Пользователям нужно перезапустить анализ для задач:")
            for task_id in blocked_tasks:
                print(f"  - {task_id}")

        return 0
    else:
        print("\n⚠️ Требуется дополнительная проверка")
        return 1

if __name__ == "__main__":
    sys.exit(main())