# Отчет по жестко закодированным значениям в Trading Assistant

## 1. КРИТИЧНЫЕ ЗНАЧЕНИЯ, КОТОРЫЕ ДОЛЖНЫ ЗАГРУЖАТЬСЯ ИЗ БАЗЫ ДАННЫХ

### 1.1. Торговые параметры (должны быть в таблице user_signal_filters для каждого пользователя)

#### Position Size и Leverage
**Проблема**: Значения по умолчанию жестко прописаны во многих местах
- **app.py:898**: `original_position_size = float(filters.get('position_size_usd', 100))`
- **app.py:899**: `original_leverage = int(filters.get('leverage', 5))`
- **app.py:1162-1163**: `position_size = 100.0`, `leverage = 5`
- **app.py:1255-1256**: `position_size = 100.0`, `leverage = 5`
- **app.py:1358-1359**: `position_size = 100.0`, `leverage = 5`
- **app.py:1568-1569**: `position_size = 100.0`, `leverage = 5`
- **app.py:3752-3753**: Fallback значения 100 и 5
- **app.py:3972-3973**: `position_size = 100`, `leverage = 5`
- **database.py:931, 2058**: Параметры функций с дефолтными значениями

#### Stop Loss и Take Profit
**Проблема**: Дефолтные значения повторяются
- **app.py:544-545**: `'stop_loss_percent': 3.00`, `'take_profit_percent': 4.00`
- **app.py:1819-1820**: `take_profit = float(data.get('take_profit', 4.0))`, `stop_loss = float(data.get('stop_loss', 3.0))`
- **app.py:4299-4300**: Аналогичные значения
- **templates/scoring_analysis.html:171,175**: Дефолты 2.0% и 1.0%

#### Trailing Stop параметры
**Проблема**: Одинаковые дефолты везде
- **app.py:547-548**: `'trailing_distance_pct': 2.0`, `'trailing_activation_pct': 1.0`
- **app.py:1164-1165**: `trailing_distance = 2.0`, `trailing_activation = 1.0`
- **app.py:1257-1258**: Те же значения
- **app.py:1361-1362**: Те же значения
- **app.py:1571-1572**: Те же значения
- **app.py:3754-3755**: Те же значения
- **database.py:932-933, 2060-2061**: Параметры функций

#### Фильтры времени
**Проблема**: Жестко заданные значения
- **app.py:543-544**: `'hide_younger_than_hours': 6`, `'hide_older_than_hours': 48`
- **app.py:1940-1941**: Дефолты 6 и 48 часов
- **app.py:4315-4316**: Те же значения
- **database.py:1680**: `hours_back=48` в функции

#### Score фильтры
**Проблема**: Минимальные значения score
- **app.py:549-550**: `'score_week_min': 0`, `'score_month_min': 0`
- **app.py:1127-1128**: Дефолты 0
- **app.py:3999-4000**: `score_week_min = 65`, `score_month_min = 64`
- **templates/efficiency_analysis_fixed.html:21-36**: value="60", value="80"
- **templates/trailing_analysis.html:44,51**: value="70"

### 1.2. Лимиты и ограничения

#### Максимум сделок за 15 минут
**Проблема**: Жестко задано
- **app.py:1343, 1499, 1553**: `max_trades_per_15min = data.get('max_trades_per_15min', 3)`
- **database.py:2533**: Параметр функции `max_trades_per_15min=3`
- **templates/**: Несколько мест с value="3"

#### Минимальная сумма сделки
**Проблема**: Жестко задана
- **app.py:443, 498**: `min_value_usd = 10000`
- **templates/dashboard.html:50**: placeholder="10000"

## 2. ЗНАЧЕНИЯ, КОТОРЫЕ ДОЛЖНЫ БЫТЬ В .ENV ФАЙЛЕ

### 2.1. Параметры подключения к БД
**Текущее состояние**: Уже частично в .env
**Проблема**: Некоторые параметры жестко заданы в коде
- **database.py:84**: `connect_timeout=10`
- **database.py:86-89**: Параметры keepalive (30, 5, 5, 60000)
- **database.py:130**: `min_size=4`
- **database.py:131**: `max_size=20`
- **database.py:132**: `timeout=30.0`
- **database.py:142-144**: max_idle, max_lifetime, max_waiting

### 2.2. Параметры повторных попыток и таймауты
- **database.py:49**: `_max_errors_before_reinit = 5`
- **database.py:50**: `_error_reset_interval = 60`
- **database.py:167**: `max_retries = 3`
- **database.py:347**: `max_attempts = 3`
- **database.py:436**: `max_retries = 3`

### 2.3. Параметры сервера
**Текущее состояние**: Частично в .env
- **app.py:4389, run.py:45**: `PORT` (уже в .env)
- **app.py:4390, run.py:46**: `FLASK_DEBUG` (уже в .env)
- **app.py:59**: `DEBUG_AUTH` проверяется, но не документирован

### 2.4. Дефолтные значения для новых пользователей
Должны быть в .env для легкой настройки:
- `DEFAULT_POSITION_SIZE=100`
- `DEFAULT_LEVERAGE=5`
- `DEFAULT_STOP_LOSS_PERCENT=3.0`
- `DEFAULT_TAKE_PROFIT_PERCENT=4.0`
- `DEFAULT_TRAILING_DISTANCE_PCT=2.0`
- `DEFAULT_TRAILING_ACTIVATION_PCT=1.0`
- `DEFAULT_HIDE_YOUNGER_HOURS=6`
- `DEFAULT_HIDE_OLDER_HOURS=48`
- `DEFAULT_MIN_VALUE_USD=10000`
- `DEFAULT_MAX_TRADES_PER_15MIN=3`

## 3. ПЛАН ИСПРАВЛЕНИЯ

### Этап 1: Миграция дефолтных значений в .env
1. Создать .env.example с новыми параметрами
2. Обновить config.py для загрузки этих значений
3. Заменить жестко заданные значения на загрузку из конфигурации

### Этап 2: Создание таблицы глобальных настроек
```sql
CREATE TABLE web.system_defaults (
    id SERIAL PRIMARY KEY,
    key VARCHAR(100) UNIQUE NOT NULL,
    value TEXT NOT NULL,
    description TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Этап 3: Рефакторинг кода
1. Создать класс ConfigManager для централизованного управления настройками
2. Заменить все жестко заданные значения на:
   - Загрузку из user_signal_filters для пользовательских настроек
   - Загрузку из system_defaults для системных дефолтов
   - Загрузку из .env для конфигурации сервера

### Этап 4: Создание UI для управления настройками
1. Страница администратора для управления system_defaults
2. Улучшить страницу пользовательских настроек

## 4. ПРИОРИТЕТ ИСПРАВЛЕНИЙ

### КРИТИЧЕСКИЙ (исправить немедленно):
1. Position size и leverage - влияют на расчеты PnL
2. Stop loss и take profit - влияют на торговую логику
3. Параметры подключения к БД - влияют на стабильность

### ВЫСОКИЙ:
1. Trailing stop параметры
2. Score фильтры
3. Временные фильтры

### СРЕДНИЙ:
1. Максимум сделок за 15 минут
2. Минимальная сумма сделки
3. Таймауты и повторные попытки

### НИЗКИЙ:
1. DEBUG параметры
2. UI дефолтные значения в шаблонах

## 5. ОЦЕНКА ВЛИЯНИЯ

- **Файлов затронуто**: 7 (app.py, database.py, run.py, config.py, .env, templates/*, models.py)
- **Строк кода для изменения**: ~200
- **Риск регрессии**: Средний (требуется тщательное тестирование)
- **Время на исправление**: 4-6 часов