#!/usr/bin/env python
"""
Тестовый скрипт для проверки работы Celery на production
"""
import sys
import time
from celery_app import celery_app
from celery_tasks import analyze_efficiency_combination

def test_celery():
    """Тестирует Celery на production"""
    print("=== Тестирование Celery на production ===")
    print("")
    
    # Проверяем зарегистрированные задачи
    print("Проверка зарегистрированных задач...")
    tasks = [t for t in celery_app.tasks.keys() if 'analyze' in t]
    
    if not tasks:
        print("✗ Задачи анализа не зарегистрированы")
        return False
        
    print(f"✓ Найдено {len(tasks)} задач:")
    for task in tasks:
        print(f"  - {task}")
    
    # Проверяем подключение к Redis
    print("\nПроверка подключения к Redis...")
    try:
        # Тестовые параметры для минимальной нагрузки
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
        
        # Проверяем, что задача принята
        time.sleep(1)
        
        if result.id:
            print(f"✓ Задача принята в Celery")
            print(f"  ID задачи: {result.id}")
            print(f"  Статус: {result.state}")
            
            # Отменяем тестовую задачу
            result.revoke(terminate=True)
            print("  Тестовая задача отменена для экономии ресурсов")
            
            print("\n✓ Celery работает корректно!")
            print("\nТеперь можно:")
            print("1. Перезапустить приложение: sudo systemctl restart trading_assistant")
            print("2. Использовать анализ эффективности без ошибок соединения")
            return True
        else:
            print("✗ Задача не была принята")
            return False
            
    except Exception as e:
        print(f"✗ Ошибка при отправке задачи: {e}")
        print("\nВозможные причины:")
        print("1. Redis не запущен (проверьте: systemctl status redis-server)")
        print("2. Celery worker не запущен (проверьте: systemctl status celery-worker)")
        print("3. Проблемы с конфигурацией в .env")
        return False

if __name__ == '__main__':
    success = test_celery()
    sys.exit(0 if success else 1)