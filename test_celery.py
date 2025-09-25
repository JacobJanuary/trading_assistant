#!/usr/bin/env python
"""
Тестовый скрипт для проверки работы Celery
"""
import sys
import time
from celery_app import celery_app
from celery_tasks import analyze_efficiency_combination

def test_celery():
    """Тестирует базовую функциональность Celery"""
    print("Тестирование Celery...")
    
    # Проверяем подключение к Redis
    try:
        # Простая тестовая задача
        @celery_app.task
        def test_task(x, y):
            return x + y
        
        # Отправляем задачу
        print("Отправка тестовой задачи...")
        result = test_task.delay(4, 6)
        
        # Ждем результат (максимум 10 секунд)
        for i in range(10):
            if result.ready():
                print(f"✓ Задача выполнена! Результат: {result.get()}")
                break
            time.sleep(1)
            print(f"  Ожидание... {i+1}/10")
        else:
            print("✗ Задача не выполнена за 10 секунд")
            print("  Проверьте, что Celery воркер запущен:")
            print("  ./start_celery_worker.sh")
            return False
            
    except Exception as e:
        print(f"✗ Ошибка: {e}")
        print("\nВозможные причины:")
        print("1. Redis не запущен (запустите: redis-server)")
        print("2. Неверные настройки в .env")
        print("3. Celery воркер не запущен")
        return False
    
    # Проверяем регистрацию задач
    print("\nПроверка зарегистрированных задач...")
    registered = list(celery_app.tasks.keys())
    our_tasks = [t for t in registered if 'analyze' in t]
    
    if our_tasks:
        print(f"✓ Найдено {len(our_tasks)} задач для анализа:")
        for task in our_tasks:
            print(f"  - {task}")
    else:
        print("✗ Задачи для анализа не зарегистрированы")
        return False
    
    print("\n✓ Celery настроен корректно!")
    return True

if __name__ == '__main__':
    success = test_celery()
    sys.exit(0 if success else 1)