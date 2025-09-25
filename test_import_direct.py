#!/usr/bin/env python
"""
Прямой тест импорта Celery SSE endpoints
"""
import sys
import traceback

print("=== Тест импорта Celery SSE endpoints ===")
print("")

# Шаг 1: Проверяем Config
try:
    from config import Config
    print(f"1. Config.USE_CELERY = {Config.USE_CELERY}")
except Exception as e:
    print(f"1. ✗ Ошибка импорта Config: {e}")
    sys.exit(1)

# Шаг 2: Проверяем celery_app
try:
    from celery_app import celery_app
    print(f"2. ✓ celery_app импортирован")
except Exception as e:
    print(f"2. ✗ Ошибка импорта celery_app: {e}")
    traceback.print_exc()

# Шаг 3: Проверяем celery_tasks
try:
    from celery_tasks import analyze_efficiency_combination
    print(f"3. ✓ celery_tasks импортирован")
except Exception as e:
    print(f"3. ✗ Ошибка импорта celery_tasks: {e}")
    traceback.print_exc()

# Шаг 4: Проверяем celery_sse_endpoints
try:
    from celery_sse_endpoints import analyze_efficiency_celery, analyze_trailing_stop_celery
    print(f"4. ✓ celery_sse_endpoints импортирован")
    print(f"   - analyze_efficiency_celery: {analyze_efficiency_celery}")
    print(f"   - analyze_trailing_stop_celery: {analyze_trailing_stop_celery}")
except Exception as e:
    print(f"4. ✗ Ошибка импорта celery_sse_endpoints: {e}")
    traceback.print_exc()

# Шаг 5: Проверяем что функция вызывается
if Config.USE_CELERY:
    try:
        # Создаем фейковый request context
        from flask import Flask, Request
        from werkzeug.test import EnvironBuilder
        from werkzeug.wrappers import Request as BaseRequest
        
        app = Flask(__name__)
        
        # Имитируем request
        with app.test_request_context('/api/efficiency/analyze_30days_progress?use_celery=true'):
            from flask import request
            
            # Имитируем current_user
            class FakeUser:
                id = 1
                username = 'test'
                
            import flask_login
            flask_login.current_user = FakeUser()
            
            print("\n5. Пробуем вызвать analyze_efficiency_celery()...")
            
            # Вызываем функцию
            result = analyze_efficiency_celery()
            print(f"   Тип результата: {type(result)}")
            print(f"   Результат: {result}")
            
    except Exception as e:
        print(f"5. ✗ Ошибка при вызове функции: {e}")
        traceback.print_exc()
else:
    print("\n5. USE_CELERY выключен, пропускаем тест вызова")