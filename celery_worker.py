"""
Celery Worker с правильной инициализацией через Application Factory
"""
from create_app import create_app, create_celery

# Создаем приложение Flask
app = create_app()

# Создаем Celery с контекстом приложения
celery = create_celery(app)

if __name__ == '__main__':
    celery.start()