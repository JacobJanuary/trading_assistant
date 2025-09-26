"""
WSGI entry point для Gunicorn
"""
from create_app import create_app

# Создаем приложение
app = create_app()

if __name__ == '__main__':
    app.run()