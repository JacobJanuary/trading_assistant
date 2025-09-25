"""
Модели данных для приложения "Помощник Трейдера"
"""
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
import database

class User(UserMixin):
    """Модель пользователя для Flask-Login"""
    
    def __init__(self, id, username, password_hash, is_admin=False, is_approved=False, created_at=None):
        self.id = id
        self.username = username
        self.password_hash = password_hash
        self.is_admin = is_admin
        self.is_approved = is_approved
        self.created_at = created_at
    
    def check_password(self, password):
        """Проверка пароля пользователя"""
        return check_password_hash(self.password_hash, password)
    
    def is_active(self):
        """Проверка активности пользователя"""
        return self.is_approved
    
    def is_authenticated(self):
        """Проверка аутентификации пользователя"""
        return True
    
    def is_anonymous(self):
        """Проверка анонимности пользователя"""
        return False
    
    def get_id(self):
        """Получение ID пользователя для Flask-Login"""
        return str(self.id)
    
    @staticmethod
    def create(db, username, password, is_admin=False, is_approved=False):
        """
        Создание нового пользователя
        
        Args:
            db: объект базы данных
            username (str): имя пользователя
            password (str): пароль в открытом виде
            is_admin (bool): является ли администратором
            is_approved (bool): подтвержден ли пользователь
            
        Returns:
            User: созданный пользователь или None при ошибке
        """
        try:
            # Проверяем, что пользователь с таким именем не существует
            existing_user = database.get_user_by_username(db, username)
            if existing_user:
                return None
            
            # Хэшируем пароль
            password_hash = generate_password_hash(password)
            
            # Создаем пользователя в базе данных
            user_id = database.create_user(db, username, password_hash, is_admin, is_approved)
            
            if user_id:
                return User.get_by_id(db, user_id)
            return None
            
        except Exception as e:
            print(f"Ошибка при создании пользователя: {e}")
            return None
    
    @staticmethod
    def get_by_id(db, user_id):
        """
        Получение пользователя по ID
        
        Args:
            db: объект базы данных
            user_id (int): ID пользователя
            
        Returns:
            User: пользователь или None
        """
        user_data = database.get_user_by_id(db, user_id)
        if user_data:
            return User(
                id=user_data['id'],
                username=user_data['username'],
                password_hash=user_data['password_hash'],
                is_admin=user_data['is_admin'],
                is_approved=user_data['is_approved'],
                created_at=user_data['created_at']
            )
        return None
    
    @staticmethod
    def get_by_username(db, username):
        """
        Получение пользователя по имени
        
        Args:
            db: объект базы данных
            username (str): имя пользователя
            
        Returns:
            User: пользователь или None
        """
        user_data = database.get_user_by_username(db, username)
        if user_data:
            return User(
                id=user_data['id'],
                username=user_data['username'],
                password_hash=user_data['password_hash'],
                is_admin=user_data['is_admin'],
                is_approved=user_data['is_approved'],
                created_at=user_data['created_at']
            )
        return None
    
    @staticmethod
    def authenticate(db, username, password):
        """
        Аутентификация пользователя
        
        Args:
            db: объект базы данных
            username (str): имя пользователя
            password (str): пароль
            
        Returns:
            User: аутентифицированный пользователь или None
        """
        user = User.get_by_username(db, username)
        if user and user.check_password(password):
            return user
        return None

class TradingData:
    """Модель для работы с данными торгов"""
    
    def __init__(self, base_asset, total_buys, total_sells, net_flow, total_trades=0):
        self.base_asset = base_asset
        self.total_buys = float(total_buys) if total_buys else 0.0
        self.total_sells = float(total_sells) if total_sells else 0.0
        self.net_flow = float(net_flow) if net_flow else 0.0
        self.total_trades = int(total_trades) if total_trades else 0
    
    def to_dict(self):
        """Преобразование в словарь для JSON"""
        return {
            'base_asset': self.base_asset,
            'total_buys': self.total_buys,
            'total_sells': self.total_sells,
            'net_flow': self.net_flow,
            'total_trades': self.total_trades
        }
    
    @staticmethod
    def get_dashboard_data(db, time_filter=None, min_value_usd=None, operation_type=None):
        """
        Получение данных для дашборда
        
        Args:
            db: объект базы данных
            time_filter (str): временной фильтр
            min_value_usd (float): минимальная сумма сделки
            operation_type (str): тип операций ('buys', 'sells', 'both')
            
        Returns:
            list: список объектов TradingData
        """
        raw_data = database.get_trading_data(db, time_filter, min_value_usd, operation_type)
        return [
            TradingData(
                base_asset=row['base_asset'],
                total_buys=row['total_buys'],
                total_sells=row['total_sells'],
                net_flow=row['net_flow'],
                total_trades=row.get('total_trades', 0)
            )
            for row in raw_data
        ]

class TradingStats:
    """Модель для статистики торгов"""
    
    def __init__(self, total_trades, total_volume, total_assets, avg_trade_size=0, max_trade_size=0):
        self.total_trades = int(total_trades) if total_trades else 0
        self.total_volume = float(total_volume) if total_volume else 0.0
        self.total_assets = int(total_assets) if total_assets else 0
        self.avg_trade_size = float(avg_trade_size) if avg_trade_size else 0.0
        self.max_trade_size = float(max_trade_size) if max_trade_size else 0.0
    
    def to_dict(self):
        """Преобразование в словарь для JSON"""
        return {
            'total_trades': self.total_trades,
            'total_volume': self.total_volume,
            'total_assets': self.total_assets,
            'avg_trade_size': self.avg_trade_size,
            'max_trade_size': self.max_trade_size
        }
    
    @staticmethod
    def get_stats(db, time_filter=None, min_value_usd=None, operation_type=None):
        """
        Получение статистики торгов
        
        Args:
            db: объект базы данных
            time_filter (str): временной фильтр
            min_value_usd (float): минимальная сумма сделки
            operation_type (str): тип операций ('buys', 'sells', 'both')
            
        Returns:
            TradingStats: объект статистики
        """
        stats_data = database.get_trading_stats(db, time_filter, min_value_usd, operation_type)
        if stats_data:
            return TradingStats(
                total_trades=stats_data['total_trades'],
                total_volume=stats_data['total_volume'],
                total_assets=stats_data['total_assets'],
                avg_trade_size=stats_data.get('avg_trade_size', 0),
                max_trade_size=stats_data.get('max_trade_size', 0)
            )
        return TradingStats(0, 0.0, 0, 0.0, 0.0)
