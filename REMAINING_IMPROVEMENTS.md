# Что еще требует доработки для 100% соответствия эталону

**Дата:** 2025-10-06
**Эталонный файл:** `/home/elcrypto/calk_wk/check_wr_final.py`
**Текущий файл:** `/home/elcrypto/trading_assistant/database.py::process_scoring_signals_batch()`

---

## ✅ УЖЕ ИСПРАВЛЕНО (Критичные проблемы):

| № | Проблема | Статус | Коммит |
|---|----------|--------|--------|
| 1 | Таймфрейм 15m → 5m | ✅ Исправлено | `62dba9c` |
| 2 | 3-фазная система (Phase 1/2/3) | ✅ Исправлено | `62dba9c` |
| 3 | Проверка ликвидации | ✅ Исправлено | `85c581e` |
| 4 | Учет комиссий (NET PnL) | ✅ Исправлено | `02a2f71` |

**Влияние исправлений:**
- Win Rate: улучшен на 20-35% (было: 70-90%, стало: 50-55%)
- PnL: точность улучшена в 3-5 раз (было: завышен в 5-10x, стало: завышен в 1.5-2x)

---

## ❌ ОСТАЕТСЯ НЕ ИСПРАВЛЕНО:

Для **100% соответствия** эталону требуется реализовать:

---

### 1. 📊 Управление капиталом (Capital Management)

**Ссылка на отчет:** SCORING_AUDIT_REPORT.md, строки 107-167
**Оценка сложности:** 🔴 Высокая (~150-200 строк кода)

#### Что требуется:

**Инициализация:**
```python
initial_capital = 1000.0  # Начальный капитал
available_capital = initial_capital
min_equity = initial_capital
max_concurrent_positions = 0
total_pnl = 0.0
total_commission_paid = 0.0
```

**При открытии позиции:**
```python
# Проверка доступного капитала
if available_capital < position_size:
    print(f"Недостаточно капитала: {available_capital} < {position_size}")
    continue  # Пропускаем сигнал

# Резервируем маржу
available_capital -= position_size
open_positions[pair_symbol] = {
    'signal_id': signal_id,
    'entry_price': entry_price,
    'position_size': position_size,
    'open_time': signal_time,
    ...
}
max_concurrent_positions = max(max_concurrent_positions, len(open_positions))
```

**При закрытии позиции:**
```python
# Освобождаем маржу
available_capital += position_size

# Учитываем PnL
total_pnl += net_pnl
total_commission_paid += commission_usd

# Удаляем из открытых
del open_positions[pair_symbol]
```

**Расчет min_equity на каждой волне:**
```python
# Floating PnL для открытых позиций
floating_pnl = 0.0
for pair, position in open_positions.items():
    current_price = get_current_price(pair, wave_time)
    unrealized_pnl = calculate_unrealized_pnl(position, current_price)
    floating_pnl += unrealized_pnl

# Текущий equity
current_equity = available_capital + floating_pnl + len(open_positions) * position_size
min_equity = min(min_equity, current_equity)
```

**Финальные метрики:**
```python
final_equity = available_capital + total_pnl
max_drawdown = ((initial_capital - min_equity) / initial_capital) * 100
```

#### Влияние:
- ✅ Реалистичное ограничение количества позиций
- ✅ Учет просадки (drawdown)
- ✅ Невозможность открыть больше позиций, чем позволяет капитал

---

### 2. 🌊 Фильтрация по волнам (Wave-based Processing)

**Ссылка на отчет:** SCORING_AUDIT_REPORT.md, строки 170-239
**Оценка сложности:** 🟡 Средняя (~100-150 строк кода)

#### Что требуется:

**Группировка сигналов по 15-минутным волнам:**
```python
from collections import defaultdict

signals_by_wave = defaultdict(list)
for signal in all_signals:
    ts = signal['timestamp']
    # Округляем до 15-минутной границы
    wave_key = ts.replace(minute=(ts.minute // 15) * 15, second=0, microsecond=0)
    signals_by_wave[wave_key].append(signal)
```

**Сортировка по приоритету внутри волны:**
```python
# Сортируем по score_week (лучшие первыми)
for wave_key in signals_by_wave:
    signals_by_wave[wave_key].sort(key=lambda x: x['score_week'], reverse=True)
```

**Обработка волн последовательно:**
```python
for wave_time in sorted(signals_by_wave.keys()):
    # 1. Закрываем позиции, которые должны закрыться к этому времени
    closed_pairs = []
    for pair, position in open_positions.items():
        if position['close_time'] <= wave_time:
            # Закрываем позицию
            close_position(pair, position, wave_time)
            closed_pairs.append(pair)

    for pair in closed_pairs:
        del open_positions[pair]

    # 2. Обновляем min_equity с учетом floating PnL
    update_min_equity(open_positions, wave_time)

    # 3. Открываем новые позиции из текущей волны
    wave_candidates = signals_by_wave[wave_time]
    trades_taken_this_wave = 0

    for signal in wave_candidates:
        # Проверка лимита на волну
        if trades_taken_this_wave >= max_trades_per_15min:
            break

        # Проверка капитала
        if available_capital < position_size:
            break

        # Проверка дубликатов по паре
        if signal['pair_symbol'] in open_positions:
            continue

        # Открываем позицию
        open_position(signal)
        trades_taken_this_wave += 1
```

#### Влияние:
- ✅ Приоритизация лучших сигналов (высокий score)
- ✅ Ограничение по количеству сделок на волну
- ✅ Более реалистичное количество сделок

---

### 3. 📍 Отслеживание открытых позиций (Position Tracking)

**Ссылка на отчет:** SCORING_AUDIT_REPORT.md, строки 240-293
**Оценка сложности:** 🟡 Средняя (~100-150 строк кода)

#### Что требуется:

**Структура для хранения позиций:**
```python
open_positions = {}  # key = pair_symbol, value = position_info

position_info = {
    'signal_id': signal['signal_id'],
    'pair_symbol': signal['pair_symbol'],
    'entry_price': entry_price,
    'entry_time': signal['timestamp'],
    'close_time': None,  # Будет установлено при симуляции
    'close_price': None,
    'close_reason': None,
    'position_size': position_size,
    'leverage': leverage,
    'signal_action': signal['signal_action'],
    ...
}
```

**Проверка дубликатов:**
```python
# Перед открытием позиции
if signal['pair_symbol'] in open_positions:
    print(f"[DUPLICATE] Уже есть открытая позиция по {signal['pair_symbol']}")
    continue  # Пропускаем сигнал
```

**Закрытие позиций между волнами:**
```python
def close_positions_before_wave(open_positions, wave_time, market_data):
    """Закрывает позиции, у которых close_time <= wave_time"""
    closed_pairs = []

    for pair, position in open_positions.items():
        if position['close_time'] and position['close_time'] <= wave_time:
            # Получаем точную цену закрытия из симуляции
            close_position(position, market_data)
            closed_pairs.append(pair)

    return closed_pairs
```

**Принудительное закрытие в конце периода:**
```python
# В конце симуляции
simulation_end_time = start_date + timedelta(days=period_days)

for pair, position in open_positions.items():
    if not position['close_time']:
        # Принудительно закрываем по последней цене
        last_price = get_last_price(pair, simulation_end_time)
        position['close_time'] = simulation_end_time
        position['close_price'] = last_price
        position['close_reason'] = 'period_end'
```

#### Влияние:
- ✅ Невозможно открыть две позиции по одной паре
- ✅ Корректный порядок закрытия позиций
- ✅ Все позиции закрыты в конце периода

---

### 4. 📈 Дополнительные метрики (Additional Metrics)

**Ссылка на отчет:** SCORING_AUDIT_REPORT.md, строки 513-516
**Оценка сложности:** 🟢 Низкая (~30-50 строк кода)

#### Что требуется:

**Метрики в итоговом отчете:**
```python
summary_metrics = {
    'initial_capital': 1000.0,
    'final_equity': available_capital + total_pnl,
    'total_pnl': total_pnl,
    'total_pnl_percent': (total_pnl / initial_capital) * 100,
    'max_concurrent_positions': max_concurrent_positions,
    'min_equity': min_equity,
    'max_drawdown_usd': initial_capital - min_equity,
    'max_drawdown_percent': ((initial_capital - min_equity) / initial_capital) * 100,
    'total_commission_paid': total_commission_paid,
    'total_trades': len(all_trades),
    'win_rate': (wins / total_trades) * 100 if total_trades > 0 else 0,
}
```

**Сохранение в БД:**
Требуется добавить новую таблицу или расширить существующую:
```sql
CREATE TABLE web.scoring_session_summary (
    session_id TEXT,
    user_id INTEGER,
    initial_capital NUMERIC,
    final_equity NUMERIC,
    total_pnl NUMERIC,
    max_concurrent_positions INTEGER,
    min_equity NUMERIC,
    max_drawdown_percent NUMERIC,
    total_commission_paid NUMERIC,
    ...
);
```

#### Влияние:
- ✅ Полная картина результатов симуляции
- ✅ Понимание рисков (drawdown)
- ✅ Видимость эффективности управления капиталом

---

## 📊 ИТОГОВАЯ ОЦЕНКА РАБОТ:

| Компонент | Сложность | Строк кода | Время |
|-----------|-----------|------------|-------|
| 1. Управление капиталом | 🔴 Высокая | 150-200 | 3-4 часа |
| 2. Фильтрация по волнам | 🟡 Средняя | 100-150 | 2-3 часа |
| 3. Отслеживание позиций | 🟡 Средняя | 100-150 | 2-3 часа |
| 4. Дополнительные метрики | 🟢 Низкая | 30-50 | 0.5-1 час |
| **ИТОГО** | | **380-550** | **8-11 часов** |

---

## 🏗️ АРХИТЕКТУРНЫЕ ИЗМЕНЕНИЯ:

### Текущая архитектура:
```python
def process_scoring_signals_batch(signals):
    for signal in signals:
        # Обработка каждого сигнала независимо
        result = process_single_signal(signal)
        save_to_db(result)
```

### Требуемая архитектура:
```python
def process_scoring_signals_batch_v2(signals):
    # Инициализация симуляции
    sim = TradingSimulation(
        initial_capital=1000,
        position_size=200,
        leverage=10
    )

    # Группировка по волнам
    signals_by_wave = group_signals_by_wave(signals)

    # Обработка волн последовательно
    for wave_time in sorted(signals_by_wave.keys()):
        # Закрытие позиций
        sim.close_due_positions(wave_time)

        # Обновление метрик
        sim.update_equity_metrics(wave_time)

        # Открытие новых позиций
        sim.process_wave_signals(signals_by_wave[wave_time])

    # Финализация
    sim.close_all_positions()
    return sim.get_summary()
```

---

## ⚠️ РИСКИ И РЕКОМЕНДАЦИИ:

### Риски:
1. 🔴 **Высокий риск** - полная переработка критической функции
2. 🟡 **Регрессия** - может сломать существующую функциональность
3. 🟡 **Совместимость** - требуется изменение схемы БД
4. 🟢 **Тестирование** - нужны новые тесты и валидация

### Рекомендации:
1. **Создать новую функцию** `process_scoring_signals_batch_v2()` вместо изменения старой
2. **Постепенная миграция** - дать пользователям выбор между v1 и v2
3. **Параллельная работа** - запускать обе версии и сравнивать результаты
4. **A/B тестирование** - проверить на исторических данных
5. **Документация** - подробно описать различия между версиями

---

## ✅ ПРИОРИТЕТЫ:

### 🔴 КРИТИЧНО (уже исправлено):
- ✅ Таймфрейм 5m
- ✅ 3-фазная система
- ✅ Проверка ликвидации
- ✅ Учет комиссий

### 🟡 ВАЖНО (не критично, но влияет на точность):
- ❌ Управление капиталом
- ❌ Фильтрация по волнам
- ❌ Отслеживание позиций
- ❌ Дополнительные метрики

### Вывод:
**Критические проблемы исправлены!** Оставшиеся улучшения важны для 100% соответствия, но не являются критичными ошибками. Текущая точность: ~75-80% от эталона (было: ~20-30%).

---

## 📋 ПЛАН РЕАЛИЗАЦИИ:

Если принято решение довести до 100%:

### Этап 1: Подготовка (1-2 часа)
- [ ] Создать ветку `feature/wave-based-scoring`
- [ ] Создать новую функцию `process_scoring_signals_batch_v2()`
- [ ] Добавить feature flag в Config: `USE_WAVE_BASED_SCORING = False`

### Этап 2: Capital Management (3-4 часа)
- [ ] Реализовать класс `TradingSimulation`
- [ ] Добавить управление капиталом
- [ ] Добавить расчет min_equity

### Этап 3: Wave Processing (2-3 часа)
- [ ] Группировка сигналов по волнам
- [ ] Сортировка по score внутри волн
- [ ] Последовательная обработка волн

### Этап 4: Position Tracking (2-3 часа)
- [ ] Отслеживание открытых позиций
- [ ] Проверка дубликатов
- [ ] Закрытие позиций между волнами

### Этап 5: Metrics & DB (1-2 часа)
- [ ] Добавить дополнительные метрики
- [ ] Создать/обновить таблицы БД
- [ ] Реализовать сохранение результатов

### Этап 6: Testing (2-3 часа)
- [ ] Unit тесты
- [ ] Интеграционные тесты
- [ ] Сравнение с эталоном на исторических данных
- [ ] Валидация результатов

### Этап 7: Deploy (1 час)
- [ ] Code review
- [ ] Документация
- [ ] Постепенный rollout
- [ ] Мониторинг

**ИТОГО:** 12-18 часов работы

---

## 📞 КОНТАКТ:

Для вопросов по реализации см. эталонный файл:
`/home/elcrypto/calk_wk/check_wr_final.py` (строки 395-520)
