#!/usr/bin/env python
"""
Локальный тестовый сервер для отладки SSE и JavaScript
Не требует Celery и Redis
"""
from flask import Flask, Response, render_template_string, request, jsonify
import json
import time
import random

app = Flask(__name__)

# Простой HTML шаблон для тестирования
TEST_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>SSE Test</title>
    <style>
        body { font-family: Arial; padding: 20px; }
        #log { 
            background: #f0f0f0; 
            padding: 10px; 
            height: 400px; 
            overflow-y: scroll;
            margin-top: 20px;
        }
        .log-entry { 
            padding: 5px; 
            border-bottom: 1px solid #ddd;
            font-family: monospace;
        }
        .error { color: red; }
        .success { color: green; }
        .info { color: blue; }
        button { 
            padding: 10px 20px; 
            margin: 10px;
            font-size: 16px;
        }
    </style>
</head>
<body>
    <h1>SSE Connection Test</h1>
    
    <div>
        <button onclick="testNormalSSE()">Test Normal SSE</button>
        <button onclick="testCelerySSE()">Test Celery-like SSE</button>
        <button onclick="testBrokenSSE()">Test Broken SSE (HTML)</button>
        <button onclick="clearLog()">Clear Log</button>
    </div>
    
    <div id="status">Status: Ready</div>
    <div id="log"></div>
    
    <script>
        let eventSource = null;
        
        function log(message, type = 'info') {
            const logDiv = document.getElementById('log');
            const entry = document.createElement('div');
            entry.className = 'log-entry ' + type;
            entry.textContent = new Date().toLocaleTimeString() + ' - ' + message;
            logDiv.appendChild(entry);
            logDiv.scrollTop = logDiv.scrollHeight;
        }
        
        function updateStatus(status) {
            document.getElementById('status').textContent = 'Status: ' + status;
        }
        
        function clearLog() {
            document.getElementById('log').innerHTML = '';
        }
        
        function closeConnection() {
            if (eventSource) {
                eventSource.close();
                eventSource = null;
                log('Connection closed', 'info');
            }
        }
        
        function testNormalSSE() {
            closeConnection();
            log('Starting normal SSE test...', 'info');
            
            const params = new URLSearchParams({
                score_week_min: 60,
                score_week_max: 80,
                use_celery: 'false',
                session_id: 'test_' + Date.now()
            });
            
            const url = '/test/normal_sse?' + params;
            log('Connecting to: ' + url, 'info');
            
            eventSource = new EventSource(url);
            
            eventSource.onopen = function(event) {
                log('✓ Connection opened', 'success');
                updateStatus('Connected');
            };
            
            eventSource.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    log('Message: ' + JSON.stringify(data), 'success');
                } catch (e) {
                    log('Raw message: ' + event.data, 'info');
                }
            };
            
            eventSource.onerror = function(error) {
                log('✗ Error: readyState=' + eventSource.readyState, 'error');
                if (eventSource.readyState === 2) {
                    updateStatus('Connection closed');
                    log('Connection closed permanently', 'error');
                }
            };
        }
        
        function testCelerySSE() {
            closeConnection();
            log('Starting Celery-like SSE test...', 'info');
            
            const params = new URLSearchParams({
                score_week_min: 60,
                score_week_max: 80,
                use_celery: 'true',
                session_id: 'test_' + Date.now()
            });
            
            const url = '/test/celery_sse?' + params;
            log('Connecting to: ' + url, 'info');
            
            eventSource = new EventSource(url);
            
            eventSource.onopen = function(event) {
                log('✓ Connection opened', 'success');
                updateStatus('Connected (Celery mode)');
            };
            
            eventSource.onmessage = function(event) {
                try {
                    const data = JSON.parse(event.data);
                    log('Celery message type=' + data.type + ': ' + JSON.stringify(data), 'success');
                } catch (e) {
                    log('Raw message: ' + event.data, 'info');
                }
            };
            
            eventSource.onerror = function(error) {
                log('✗ Error: readyState=' + eventSource.readyState, 'error');
                if (eventSource.readyState === 2) {
                    updateStatus('Connection closed');
                }
            };
        }
        
        function testBrokenSSE() {
            closeConnection();
            log('Starting broken SSE test (will return HTML)...', 'info');
            
            const url = '/test/broken_sse';
            log('Connecting to: ' + url, 'info');
            
            try {
                eventSource = new EventSource(url);
                
                eventSource.onopen = function(event) {
                    log('Connection opened (should not happen)', 'error');
                };
                
                eventSource.onerror = function(error) {
                    log('✗ Expected error: MIME type is not text/event-stream', 'error');
                    log('readyState=' + eventSource.readyState, 'error');
                    updateStatus('Failed as expected');
                };
            } catch (e) {
                log('Exception: ' + e.message, 'error');
            }
        }
    </script>
</body>
</html>
"""

@app.route('/')
def index():
    return TEST_HTML

@app.route('/test/normal_sse')
def test_normal_sse():
    """Normal SSE endpoint"""
    def generate():
        for i in range(5):
            data = {
                'type': 'progress',
                'percent': i * 20,
                'message': f'Processing step {i+1}/5'
            }
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(1)
        
        yield f"data: {json.dumps({'type': 'complete', 'message': 'Done'})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/test/celery_sse')
def test_celery_sse():
    """Celery-like SSE endpoint"""
    def generate():
        # Имитируем Celery workflow
        yield f"data: {json.dumps({'type': 'tasks_started', 'count': 10})}\n\n"
        time.sleep(1)
        
        for i in range(10):
            data = {
                'type': 'combination_complete',
                'combination_id': i+1,
                'result': {
                    'pnl': random.randint(-100, 300),
                    'signals': random.randint(5, 20)
                }
            }
            yield f"data: {json.dumps(data)}\n\n"
            time.sleep(0.5)
        
        yield f"data: {json.dumps({'type': 'complete', 'results': 'final_data'})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

@app.route('/test/broken_sse')
def test_broken_sse():
    """Broken endpoint that returns HTML instead of SSE"""
    return "<html><body>Error: This is HTML, not SSE</body></html>", 500

@app.route('/api/efficiency/analyze_30days_progress')
def mock_efficiency_endpoint():
    """Mock efficiency endpoint для тестирования реального JavaScript"""
    use_celery = request.args.get('use_celery', 'false')
    
    def generate():
        yield f"data: {json.dumps({'type': 'start', 'message': f'Starting (celery={use_celery})', 'total_combinations': 9})}\n\n"
        
        if use_celery == 'true':
            yield f"data: {json.dumps({'type': 'tasks_started', 'count': 9})}\n\n"
            time.sleep(1)
            
            for i in range(9):
                yield f"data: {json.dumps({'type': 'combination_complete', 'combination_id': i+1, 'result': {'pnl': 100}})}\n\n"
                time.sleep(0.2)
        else:
            for i in range(9):
                yield f"data: {json.dumps({'type': 'progress', 'percent': (i+1)*11, 'current': i+1, 'total': 9})}\n\n"
                time.sleep(0.5)
        
        yield f"data: {json.dumps({'type': 'complete', 'results': []})}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')

if __name__ == '__main__':
    print("\n" + "="*50)
    print("SSE Test Server")
    print("="*50)
    print("\nOpen in browser:")
    print("  http://localhost:5556/")
    print("\nTest the actual efficiency_analysis.html:")
    print("  1. Copy efficiency_analysis.html to templates/ folder")
    print("  2. Open http://localhost:5556/efficiency")
    print("\n" + "="*50 + "\n")
    
    app.run(port=5556, debug=True)