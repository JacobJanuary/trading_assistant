#!/usr/bin/env python3
"""
Скрипт мониторинга production окружения Trading Assistant
"""
import os
import sys
import time
import psutil
import subprocess
from datetime import datetime
from dotenv import load_dotenv

# Загрузка переменных окружения
load_dotenv('.env.production' if os.path.exists('.env.production') else '.env')

def check_gunicorn_processes():
    """Проверка процессов Gunicorn"""
    gunicorn_processes = []
    for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_info']):
        try:
            if 'gunicorn' in ' '.join(proc.info['cmdline'] or []):
                gunicorn_processes.append({
                    'pid': proc.info['pid'],
                    'memory_mb': proc.info['memory_info'].rss / 1024 / 1024
                })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return gunicorn_processes

def check_database_connections():
    """Проверка соединений к базе данных"""
    try:
        from database import Database
        db = Database(
            host=os.getenv('DB_HOST'),
            port=os.getenv('DB_PORT', '5432'),
            database=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            use_pool=False
        )
        
        # Проверка активных соединений
        result = db.execute_query("""
            SELECT 
                count(*) as total,
                count(*) FILTER (WHERE state = 'active') as active,
                count(*) FILTER (WHERE state = 'idle') as idle,
                count(*) FILTER (WHERE state = 'idle in transaction') as idle_in_transaction
            FROM pg_stat_activity 
            WHERE datname = %s
        """, (os.getenv('DB_NAME'),), fetch=True)
        
        db.close()
        return result[0] if result else None
        
    except Exception as e:
        return {'error': str(e)}

def check_ssl_connections():
    """Проверка SSL соединений"""
    try:
        from database import Database
        db = Database(
            host=os.getenv('DB_HOST'),
            port=os.getenv('DB_PORT', '5432'),
            database=os.getenv('DB_NAME'),
            user=os.getenv('DB_USER'),
            use_pool=False
        )
        
        result = db.execute_query("""
            SELECT 
                count(*) as total,
                count(*) FILTER (WHERE ssl = true) as with_ssl,
                count(*) FILTER (WHERE ssl = false) as without_ssl
            FROM pg_stat_ssl
        """, fetch=True)
        
        db.close()
        return result[0] if result else None
        
    except Exception as e:
        return {'error': str(e)}

def check_system_resources():
    """Проверка системных ресурсов"""
    return {
        'cpu_percent': psutil.cpu_percent(interval=1),
        'memory_percent': psutil.virtual_memory().percent,
        'memory_available_mb': psutil.virtual_memory().available / 1024 / 1024,
        'disk_usage_percent': psutil.disk_usage('/').percent
    }

def check_application_health():
    """Проверка доступности приложения"""
    try:
        result = subprocess.run(
            ['curl', '-s', '-o', '/dev/null', '-w', '%{http_code}', 'http://localhost:5000/'],
            capture_output=True,
            text=True,
            timeout=5
        )
        return result.stdout == '302' or result.stdout == '200'
    except:
        return False

def check_log_errors():
    """Проверка последних ошибок в логах"""
    errors = []
    log_file = 'logs/gunicorn_error.log'
    
    if os.path.exists(log_file):
        try:
            with open(log_file, 'r') as f:
                lines = f.readlines()
                for line in lines[-100:]:  # Последние 100 строк
                    if 'ERROR' in line or 'CRITICAL' in line or 'decryption failed' in line:
                        errors.append(line.strip())
        except:
            pass
    
    return errors[-10:]  # Возвращаем последние 10 ошибок

def print_status(title, status, details=None):
    """Вывод статуса с форматированием"""
    status_icon = "✅" if status else "❌"
    print(f"\n{status_icon} {title}")
    if details:
        for key, value in details.items():
            print(f"   {key}: {value}")

def main():
    """Главная функция мониторинга"""
    print("=" * 60)
    print(f"Trading Assistant Production Monitor")
    print(f"Время: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 60)
    
    # 1. Проверка процессов Gunicorn
    gunicorn = check_gunicorn_processes()
    print_status(
        "Процессы Gunicorn",
        len(gunicorn) > 0,
        {
            'Количество процессов': len(gunicorn),
            'Общая память (MB)': f"{sum(p['memory_mb'] for p in gunicorn):.1f}" if gunicorn else "0"
        }
    )
    
    # 2. Проверка соединений к БД
    db_conn = check_database_connections()
    if 'error' not in db_conn:
        print_status(
            "Соединения к базе данных",
            True,
            {
                'Всего': db_conn.get('total', 0),
                'Активные': db_conn.get('active', 0),
                'Idle': db_conn.get('idle', 0),
                'Idle in transaction': db_conn.get('idle_in_transaction', 0)
            }
        )
    else:
        print_status("Соединения к базе данных", False, {'Ошибка': db_conn['error']})
    
    # 3. Проверка SSL
    ssl_conn = check_ssl_connections()
    if ssl_conn and 'error' not in ssl_conn:
        print_status(
            "SSL соединения",
            True,
            {
                'Всего соединений': ssl_conn.get('total', 0),
                'С SSL': ssl_conn.get('with_ssl', 0),
                'Без SSL': ssl_conn.get('without_ssl', 0)
            }
        )
    else:
        print_status("SSL соединения", False, {'Ошибка': ssl_conn.get('error', 'неизвестно')})
    
    # 4. Системные ресурсы
    resources = check_system_resources()
    print_status(
        "Системные ресурсы",
        resources['memory_percent'] < 90 and resources['cpu_percent'] < 90,
        {
            'CPU': f"{resources['cpu_percent']}%",
            'Память': f"{resources['memory_percent']}%",
            'Доступно памяти': f"{resources['memory_available_mb']:.1f} MB",
            'Диск': f"{resources['disk_usage_percent']}%"
        }
    )
    
    # 5. Доступность приложения
    app_health = check_application_health()
    print_status(
        "Приложение",
        app_health,
        {'Статус': 'Доступно' if app_health else 'Недоступно'}
    )
    
    # 6. Ошибки в логах
    errors = check_log_errors()
    if errors:
        print_status(
            "Последние ошибки",
            False,
            {f"Ошибка {i+1}": err[:100] + "..." if len(err) > 100 else err 
             for i, err in enumerate(errors[-3:])}  # Показываем только 3 последние
        )
    else:
        print_status("Логи", True, {'Статус': 'Ошибок не найдено'})
    
    # Итоговый статус
    print("\n" + "=" * 60)
    overall_health = (
        len(gunicorn) > 0 and 
        app_health and 
        resources['memory_percent'] < 90 and
        len(errors) == 0
    )
    
    if overall_health:
        print("✅ СИСТЕМА РАБОТАЕТ НОРМАЛЬНО")
    else:
        print("⚠️  ОБНАРУЖЕНЫ ПРОБЛЕМЫ")
        if len(gunicorn) == 0:
            print("   - Gunicorn не запущен")
        if not app_health:
            print("   - Приложение недоступно")
        if resources['memory_percent'] >= 90:
            print("   - Высокое использование памяти")
        if errors:
            print("   - Есть ошибки в логах")
    
    print("=" * 60)
    
    return 0 if overall_health else 1

if __name__ == "__main__":
    sys.exit(main())