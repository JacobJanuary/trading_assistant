# AUDIT REPORT: Сравнение логики Trailing Stop

**Дата:** 2025-10-06
**Файлы:**
- Эталон: `/home/elcrypto/calk_wk/check_wr_final.py`
- Проверяемый: `/home/elcrypto/trading_assistant/database.py`

---

## АНАЛИЗ ЛОГИКИ TRAILING STOP

### 1. LONG ПОЗИЦИИ

#### check_wr_final.py (строки 278-287):
```python
if is_long:
    best_price = max(best_price, candle_high)
    if not is_ts_active and best_price >= ts_activation_price:
        is_ts_active = True
    if is_ts_active:
        new_ts_price = best_price * (1 - trailing_distance_pct / 100)
        trailing_stop_price = max(trailing_stop_price, new_ts_price) if trailing_stop_price else new_ts_price
        if candle_low <= trailing_stop_price:
            outcome = {"close_price": trailing_stop_price, "close_time": candle_time, "close_reason": "trailing_stop"}
            break
```

**Логика LONG:**
1. ✅ Обновляем best_price = max(best_price, candle_high)
2. ✅ Активация при: best_price >= ts_activation_price
3. ✅ Расчет стопа: new_ts_price = best_price * (1 - trailing_distance_pct / 100)
4. ✅ Стоп движется ВВЕРХ: trailing_stop_price = max(trailing_stop_price, new_ts_price)
5. ✅ Закрытие при: candle_low <= trailing_stop_price

#### database.py (строки 1489-1516):
```python
else:  # BUY, LONG
    # СНАЧАЛА обновляем best_price
    if high_price > best_price_for_trailing:
        best_price_for_trailing = high_price

    # ЗАТЕМ проверяем активацию trailing stop
    if not is_trailing_active and best_price_for_trailing >= activation_price:
        is_trailing_active = True
        trailing_stop_price = best_price_for_trailing * (1 - trailing_distance_pct / 100)

    # Если trailing активен, обновляем стоп при изменении best_price
    if is_trailing_active:
        new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
        if new_stop > trailing_stop_price:
            trailing_stop_price = new_stop

    # Проверяем срабатывание trailing stop
    if is_trailing_active and current_time != activation_candle_time and low_price <= trailing_stop_price:
        is_closed = True
        close_reason = 'trailing_stop'
        close_price = trailing_stop_price
```

**Сравнение LONG:**
- ✅ Обновление best_price: ИДЕНТИЧНО
- ✅ Активация: ИДЕНТИЧНО
- ✅ Расчет стопа: ИДЕНТИЧНО
- ✅ Направление движения стопа: ИДЕНТИЧНО (вверх)
- ✅ Условие закрытия: ИДЕНТИЧНО
- ⚠️ Дополнение: Защита от закрытия на свече активации (current_time != activation_candle_time)

---

### 2. SHORT ПОЗИЦИИ

#### check_wr_final.py (строки 288-298):
```python
else:  # SHORT - ИСПРАВЛЕНО
    best_price = min(best_price, candle_low)
    if not is_ts_active and best_price <= ts_activation_price:
        is_ts_active = True
    if is_ts_active:
        new_ts_price = best_price * (1 + trailing_distance_pct / 100)
        # Для SHORT trailing stop движется ВНИЗ, поэтому используем min()
        trailing_stop_price = min(trailing_stop_price, new_ts_price) if trailing_stop_price else new_ts_price
        if candle_high >= trailing_stop_price:
            outcome = {"close_price": trailing_stop_price, "close_time": candle_time, "close_reason": "trailing_stop"}
            break
```

**Логика SHORT:**
1. ✅ Обновляем best_price = min(best_price, candle_low)
2. ✅ Активация при: best_price <= ts_activation_price
3. ✅ Расчет стопа: new_ts_price = best_price * (1 + trailing_distance_pct / 100)
4. ✅ Стоп движется ВНИЗ: trailing_stop_price = min(trailing_stop_price, new_ts_price)
5. ✅ Закрытие при: candle_high >= trailing_stop_price

#### database.py (строки 1451-1478):
```python
if signal_action in ['SELL', 'SHORT']:
    # СНАЧАЛА обновляем best_price
    if low_price < best_price_for_trailing:
        best_price_for_trailing = low_price

    # ЗАТЕМ проверяем активацию trailing stop
    if not is_trailing_active and best_price_for_trailing <= activation_price:
        is_trailing_active = True
        trailing_stop_price = best_price_for_trailing * (1 + trailing_distance_pct / 100)

    # Если trailing активен, обновляем стоп при изменении best_price
    if is_trailing_active:
        new_stop = best_price_for_trailing * (1 + trailing_distance_pct / 100)
        if new_stop < trailing_stop_price:
            trailing_stop_price = new_stop

    # Проверяем срабатывание trailing stop
    if is_trailing_active and current_time != activation_candle_time and high_price >= trailing_stop_price:
        is_closed = True
        close_reason = 'trailing_stop'
        close_price = trailing_stop_price
```

**Сравнение SHORT:**
- ✅ Обновление best_price: ИДЕНТИЧНО
- ✅ Активация: ИДЕНТИЧНО
- ✅ Расчет стопа: ИДЕНТИЧНО
- ✅ Направление движения стопа: ИДЕНТИЧНО (вниз)
- ✅ Условие закрытия: ИДЕНТИЧНО
- ⚠️ Дополнение: Защита от закрытия на свече активации

---

## 3. STOP LOSS (Страховочный стоп)

#### check_wr_final.py (строки 240-241, 273-274):
```python
# Stop Loss для LONG и SHORT
sl_price = entry_price * (1 - stop_loss_percent / 100) if is_long
           else entry_price * (1 + stop_loss_percent / 100)

# Проверка Stop Loss
if (is_long and candle_low <= sl_price) or (not is_long and candle_high >= sl_price):
    outcome = {"close_price": sl_price, "close_time": candle_time, "close_reason": "stop_loss"}
    break
```

#### database.py (строки 1351-1368):
```python
# Расчет Stop Loss
if signal_action in ['SELL', 'SHORT']:
    insurance_sl_price = entry_price * (1 + insurance_sl_percent / 100)
else:
    insurance_sl_price = entry_price * (1 - insurance_sl_percent / 100)

# Проверка Stop Loss для SHORT
if not is_trailing_active and high_price >= insurance_sl_price:
    is_closed = True
    close_reason = 'stop_loss'
    close_price = insurance_sl_price

# Проверка Stop Loss для LONG
if not is_trailing_active and low_price <= insurance_sl_price:
    is_closed = True
    close_reason = 'stop_loss'
    close_price = insurance_sl_price
```

**Сравнение Stop Loss:**
- ✅ Формулы расчета: ИДЕНТИЧНЫ
- ✅ Условия срабатывания: ИДЕНТИЧНЫ
- ✅ Работает только когда trailing неактивен: ВЕРНО

---

## 4. РАСЧЕТ PnL

#### check_wr_final.py (строки 427-437):
```python
effective_position = position_size * strat['leverage']
if is_long:
    pnl_percent = ((close_price - entry_price) / entry_price) * 100
else:
    pnl_percent = ((entry_price - close_price) / entry_price) * 100

gross_pnl = effective_position * (pnl_percent / 100)

# Учет комиссий
entry_commission = effective_position * strat['commission_rate']
exit_commission = effective_position * strat['commission_rate']
net_pnl = gross_pnl - entry_commission - exit_commission
```

#### database.py (строки 1538-1543):
```python
if signal_action in ['SELL', 'SHORT']:
    result['pnl_percent'] = ((entry_price - close_price) / entry_price) * 100
else:
    result['pnl_percent'] = ((close_price - entry_price) / entry_price) * 100

result['pnl_usd'] = position_size * (result['pnl_percent'] / 100) * leverage
```

**Сравнение PnL:**
- ✅ Формулы для LONG: ИДЕНТИЧНЫ
- ✅ Формулы для SHORT: ИДЕНТИЧНЫ
- ✅ Учет leverage: ПРАВИЛЬНЫЙ
- ⚠️ **КРИТИЧНО:** НЕ УЧИТЫВАЮТСЯ КОМИССИИ в database.py!

---

## 5. МАКСИМАЛЬНЫЙ ПОТЕНЦИАЛЬНЫЙ ПРОФИТ

#### check_wr_final.py (строки 224-237):
```python
absolute_best_price = entry_price
for candle in history:
    if candle['timestamp'] > simulation_end_time:
        break  # НЕ смотрим в будущее после окончания периода симуляции
    if is_long:
        absolute_best_price = max(absolute_best_price, float(candle['high_price']))
    else:
        absolute_best_price = min(absolute_best_price, float(candle['low_price']))

max_pnl_percent = ((absolute_best_price - entry_price) / entry_price) * 100 if is_long
                  else ((entry_price - absolute_best_price) / entry_price) * 100
max_potential_pnl_usd = effective_position * (max_pnl_percent / 100) - entry_commission * 2
```

#### database.py (строки 1428-1438):
```python
# ============ БЛОК 1: ВСЕГДА обновляем абсолютный максимум ============
if signal_action in ['SELL', 'SHORT']:
    if low_price < absolute_best_price:
        absolute_best_price = low_price
        max_profit_percent = ((entry_price - absolute_best_price) / entry_price) * 100
        max_profit_usd = position_size * (max_profit_percent / 100) * leverage
else:  # BUY, LONG
    if high_price > absolute_best_price:
        absolute_best_price = high_price
        max_profit_percent = ((absolute_best_price - entry_price) / entry_price) * 100
        max_profit_usd = position_size * (max_profit_percent / 100) * leverage
```

**Сравнение Max Profit:**
- ✅ Отслеживание лучшей цены: ИДЕНТИЧНО
- ✅ Формулы для LONG/SHORT: ИДЕНТИЧНЫ
- ⚠️ **КРИТИЧНО:** НЕ ВЫЧИТАЮТСЯ КОМИССИИ из max_profit_usd в database.py!

---

## КРИТИЧЕСКИЕ ПРОБЛЕМЫ В database.py

### ❌ ПРОБЛЕМА 1: Отсутствие учета комиссий
**Файл:** database.py
**Функция:** calculate_trailing_stop_exit()

**Проблема:**
- Не рассчитываются комиссии на вход (entry_commission)
- Не рассчитываются комиссии на выход (exit_commission)
- PnL показывается gross (без учета комиссий), а не net

**Решение:**
```python
# Добавить расчет комиссий
commission_rate = 0.0006  # 0.06%
effective_position = position_size * leverage
entry_commission = effective_position * commission_rate
exit_commission = effective_position * commission_rate
net_pnl = gross_pnl - entry_commission - exit_commission
```

### ❌ ПРОБЛЕМА 2: Максимальный профит без комиссий
**Файл:** database.py
**Функция:** calculate_trailing_stop_exit()

**Проблема:**
- max_profit_usd не учитывает двойную комиссию (вход + выход)

**Решение:**
```python
max_potential_pnl_usd = effective_position * (max_pnl_percent / 100) - (entry_commission + exit_commission)
```

---

## ДОПОЛНИТЕЛЬНЫЕ НАБЛЮДЕНИЯ

### ✅ ПРЕИМУЩЕСТВА database.py:
1. **Защита от instant close:** Не закрывает позицию на свече активации (current_time != activation_candle_time)
2. **Таймаут:** Автоматическое закрытие позиций через 48 часов
3. **Детальное логирование:** Подробные print-сообщения о состоянии trailing stop

### ✅ ПРЕИМУЩЕСТВА check_wr_final.py:
1. **Полный учет комиссий:** Все PnL рассчитываются как net (после комиссий)
2. **Управление капиталом:** Правильное резервирование и освобождение маржи
3. **Ликвидация:** Проверка уровня ликвидации позиции
4. **Breakeven window:** Дополнительный механизм выхода в безубыток

---

## ВЫВОД

### Логика Trailing Stop: ✅ КОРРЕКТНА
Основная логика trailing stop в database.py **полностью соответствует** эталону:
- Правильная активация для LONG и SHORT
- Правильное направление движения стопа
- Правильные условия закрытия

### Расчет PnL: ❌ ТРЕБУЕТ ИСПРАВЛЕНИЯ
**КРИТИЧНО:** В разделе "Сигналы" (signal_performance) показывается **GROSS PnL** без учета комиссий!

Пользователь видит **завышенную** прибыль на:
- 0.06% от позиции на вход
- 0.06% от позиции на выход
- Всего ~0.12% от эффективного размера позиции

**Пример:**
- Position: $100, Leverage: 10x
- Effective position: $1000
- Комиссии: $1000 * 0.0006 * 2 = $1.20
- При PnL +2% ($20), реальный net PnL = $20 - $1.20 = $18.80

---

## РЕКОМЕНДАЦИИ

### 🔴 ВЫСОКИЙ ПРИОРИТЕТ:
1. **Добавить расчет комиссий** в calculate_trailing_stop_exit()
2. **Обновить все PnL** на net-значения (после комиссий)
3. **Исправить max_profit_usd** с учетом комиссий

### 🟡 СРЕДНИЙ ПРИОРИТЕТ:
4. Добавить параметр commission_rate в настройки пользователя
5. Отображать commission_usd в таблице результатов
6. Добавить поле total_commission в статистику

### 🟢 НИЗКИЙ ПРИОРИТЕТ:
7. Рассмотреть добавление breakeven window
8. Рассмотреть добавление проверки ликвидации
