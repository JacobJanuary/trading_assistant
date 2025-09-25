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
        # Используем существующую задачу analyze_efficiency_combination для теста
        from celery_tasks import analyze_efficiency_combination
        
        # Тестовые параметры
        test_params = {
            'date': '2024-01-01',
            'score_week_min': 50,
            'score_month_min': 50,
            'use_trailing_stop': False,
            'max_trades_per_15min': 3,
            'stop_loss': 3.0,
            'take_profit': 4.0,
            'trailing_distance': 2.0,
            'trailing_activation': 1.0
        }
        
        # Отправляем задачу
        print("Отправка тестовой задачи analyze_efficiency_combination...")
        result = analyze_efficiency_combination.delay(test_params)
        
        # Проверяем статус (не ждем выполнения, просто проверяем что задача принята)
        time.sleep(1)
        
        if result.id:
            print(f"✓ Задача принята! ID: {result.id}")
            print(f"  Статус: {result.state}")
            
            # Отменяем задачу, так как это только тест
            result.revoke(terminate=True)
            print("  Тестовая задача отменена")
            return True
        else:
            print("✗ Задача не была принята")
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