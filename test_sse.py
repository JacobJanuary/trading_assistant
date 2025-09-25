#!/usr/bin/env python
"""
Тестовый скрипт для проверки SSE и Celery
"""
from flask import Flask, Response
from config import Config
import json
import time

app = Flask(__name__)

@app.route('/test/sse')
def test_sse():
    """Простой тест SSE"""
    def generate():
        for i in range(5):
            yield f"data: {json.dumps({'count': i, 'message': f'Test {i}'})}\n\n"
            time.sleep(1)
        yield f"data: {json.dumps({'type': 'complete', 'message': 'Test complete'})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/test/celery')
def test_celery():
    """Тест Celery конфигурации"""
    result = {
        'USE_CELERY': Config.USE_CELERY,
        'BROKER': Config.CELERY_BROKER_URL,
        'tasks': []
    }
    
    if Config.USE_CELERY:
        try:
            from celery_app import celery_app
            result['tasks'] = [t for t in celery_app.tasks.keys() if 'analyze' in t]
            result['celery_status'] = 'OK'
        except Exception as e:
            result['celery_status'] = f'Error: {str(e)}'
    
    return json.dumps(result, indent=2)

@app.route('/test/celery_sse')
def test_celery_sse():
    """Тест SSE с Celery"""
    if not Config.USE_CELERY:
        return "Celery disabled", 400
        
    def generate():
        try:
            yield f"data: {json.dumps({'type': 'start', 'USE_CELERY': Config.USE_CELERY})}\n\n"
            
            from celery_sse_endpoints import analyze_efficiency_celery
            yield f"data: {json.dumps({'type': 'import', 'message': 'celery_sse_endpoints imported'})}\n\n"
            
            from celery_tasks import analyze_efficiency_combination
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
            
            result = analyze_efficiency_combination.delay(test_params)
            yield f"data: {json.dumps({'type': 'task', 'task_id': result.id})}\n\n"
            
            # Ждем результат
            for i in range(10):
                if result.ready():
                    yield f"data: {json.dumps({'type': 'result', 'state': result.state})}\n\n"
                    break
                yield f"data: {json.dumps({'type': 'wait', 'seconds': i})}\n\n"
                time.sleep(1)
            
            # Отменяем задачу
            result.revoke(terminate=True)
            yield f"data: {json.dumps({'type': 'complete', 'message': 'Test complete'})}\n\n"
            
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    print(f"Starting test server...")
    print(f"USE_CELERY: {Config.USE_CELERY}")
    print(f"Open browser:")
    print("  http://localhost:5555/test/sse - простой SSE тест")
    print("  http://localhost:5555/test/celery - проверка Celery")
    print("  http://localhost:5555/test/celery_sse - тест SSE с Celery")
    app.run(port=5555, debug=True)