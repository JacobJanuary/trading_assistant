# 🔧 ПЛАН ИСПРАВЛЕНИЙ - ISOLATED MARGIN

## ✅ ПРАВИЛЬНОЕ ПОНИМАНИЕ ISOLATED MARGIN

При изолированной марже:
- **Максимальный убыток = размер маржи** (например, $100 для позиции $100)
- Убытки НЕ МОГУТ превысить маржу
- При достижении убытка ~90% от маржи происходит ликвидация
- Остальной капитал защищен от убытков этой позиции

## 🚨 ПРОБЛЕМА В ТЕКУЩЕЙ РЕАЛИЗАЦИИ

**Текущий код НЕ ОГРАНИЧИВАЕТ убытки размером маржи!** Это критическая ошибка.

В эталонной реализации (`backtest_both.py`) есть функция `cap_loss_to_margin()`:

```python
def cap_loss_to_margin(gross_pnl, entry_commission, exit_commission, position_margin):
    """
    Ограничивает убыток размером изолированной маржи.
    Максимальный убыток = position_margin - entry_commission
    """
    total_commission = entry_commission + exit_commission
    net_pnl = gross_pnl - total_commission
    max_loss = -(position_margin - entry_commission)
    return max(net_pnl, max_loss)
```

**Эта функция ОТСУТСТВУЕТ в текущей реализации!**

---

## 📋 ПЛАН ИСПРАВЛЕНИЙ (ПО ПРИОРИТЕТУ)

### 1️⃣ КРИТИЧНО: Добавить функцию cap_loss_to_margin()

**Файл:** `/home/elcrypto/trading_assistant/trading_simulation.py`

```python
def cap_loss_to_margin(self, gross_pnl, entry_commission, exit_commission, position_margin):
    """
    Ограничивает убыток размером изолированной маржи

    Args:
        gross_pnl: Валовая прибыль/убыток
        entry_commission: Комиссия при входе
        exit_commission: Комиссия при выходе
        position_margin: Размер маржи позиции

    Returns:
        float: PnL, ограниченный размером маржи
    """
    total_commission = entry_commission + exit_commission
    net_pnl = gross_pnl - total_commission

    # Максимальный убыток = маржа минус комиссия входа
    # (комиссия входа уже была списана при открытии)
    max_loss = -(position_margin - entry_commission)

    # Ограничиваем убыток
    return max(net_pnl, max_loss)
```

### 2️⃣ КРИТИЧНО: Применить cap_loss_to_margin() во ВСЕХ местах закрытия позиций

#### Место 1: `_simulate_fixed_tp_sl()` (строка ~291)
```python
# БЫЛО:
gross_pnl = effective_position * (pnl_percent / 100)
pnl_usd = gross_pnl - total_commission  # БЕЗ ОГРАНИЧЕНИЯ!

# ДОЛЖНО БЫТЬ:
gross_pnl = effective_position * (pnl_percent / 100)
net_pnl = gross_pnl - total_commission
pnl_usd = self.cap_loss_to_margin(gross_pnl, entry_commission,
                                  exit_commission, self.position_size)
```

#### Место 2: `calculate_trailing_stop_exit()` в database.py (строка ~1610)
```python
# БЫЛО:
net_pnl = gross_pnl - total_commission

# ДОЛЖНО БЫТЬ:
net_pnl = gross_pnl - total_commission
# Ограничиваем убыток размером маржи
max_loss = -(position_size - entry_commission)
net_pnl = max(net_pnl, max_loss)
```

#### Место 3: `force_close_all_positions()` (строка ~451)
```python
# БЫЛО:
net_pnl = gross_pnl - total_commission

# ДОЛЖНО БЫТЬ:
net_pnl = self.cap_loss_to_margin(gross_pnl, entry_commission,
                                  exit_commission, self.position_size)
```

#### Место 4: `close_due_positions()` (строка ~391)
```python
# БЫЛО:
self.available_capital += position['position_size'] + net_pnl

# ДОЛЖНО БЫТЬ:
capped_pnl = self.cap_loss_to_margin(gross_pnl, entry_commission,
                                     exit_commission, position['position_size'])
self.available_capital += position['position_size'] + capped_pnl
```

### 3️⃣ КРИТИЧНО: Исправить расчет Floating PnL

**Файл:** `database.py`, строка 2947

```python
# БЫЛО:
sim.update_equity_metrics(wave_time, market_data_by_pair=None)

# ДОЛЖНО БЫТЬ:
# Подготовить market_data для открытых позиций
market_data_by_pair = {}
for pair, position in sim.open_positions.items():
    trading_pair_id = signal.get('trading_pair_id')
    market_data = get_market_data(trading_pair_id, signal['timestamp'])
    if market_data:
        market_data_by_pair[pair] = market_data

sim.update_equity_metrics(wave_time, market_data_by_pair)
```

**И в `calculate_floating_pnl()`:**
```python
# Добавить ограничение floating убытка
floating_pnl = effective_position * (pnl_percent / 100)

# Ограничить убыток 95% маржи (5% резерв на комиссии)
max_floating_loss = -position_size * 0.95
floating_pnl = max(floating_pnl, max_floating_loss)
```

### 4️⃣ ВАЖНО: Исправить Trailing Stop Logic

**Файл:** `database.py`, строка 1575-1577

```python
# БЫЛО (НЕПРАВИЛЬНО):
if is_long:
    if new_stop > trailing_stop_price:  # ОШИБКА!
        trailing_stop_price = new_stop
else:
    if new_stop < trailing_stop_price:  # ОШИБКА!
        trailing_stop_price = new_stop

# ДОЛЖНО БЫТЬ:
if is_long:
    # Для LONG: trailing stop может только ПОВЫШАТЬСЯ (но цена движется вниз к нему)
    if trailing_stop_price is None or new_stop > trailing_stop_price:
        trailing_stop_price = new_stop
else:
    # Для SHORT: trailing stop может только ПОНИЖАТЬСЯ (но цена движется вверх к нему)
    if trailing_stop_price is None or new_stop < trailing_stop_price:
        trailing_stop_price = new_stop
```

### 5️⃣ ВАЖНО: Добавить Slippage на Stop Loss

**Файлы:** `trading_simulation.py` и `database.py`

```python
# В конфигурации
SLIPPAGE_PERCENT = 0.05  # 0.05%

# При исполнении stop loss:
if is_long:
    actual_sl_price = sl_price * (1 - SLIPPAGE_PERCENT / 100)
else:
    actual_sl_price = sl_price * (1 + SLIPPAGE_PERCENT / 100)
```

### 6️⃣ ДОПОЛНИТЕЛЬНО: Добавить проверку ликвидации

```python
def check_liquidation(self, unrealized_pnl_pct, position_margin, leverage):
    """
    Проверка условий ликвидации

    При isolated margin ликвидация происходит когда:
    убыток достигает 90% от маржи (10% остается на fees)
    """
    liquidation_threshold = 0.9  # 90% от маржи
    max_loss_pct = (100 / leverage) * liquidation_threshold

    if unrealized_pnl_pct <= -max_loss_pct:
        return True, "liquidation"
    return False, None
```

---

## 📊 ОЖИДАЕМЫЙ РЕЗУЛЬТАТ ПОСЛЕ ИСПРАВЛЕНИЙ

### ДО исправлений:
- ❌ Убытки могут быть -$200, -$300 на $100 позицию
- ❌ Equity может стать отрицательным
- ❌ Floating PnL не учитывается
- ❌ Trailing stop работает наоборот
- ❌ Нет slippage (завышенная прибыль)

### ПОСЛЕ исправлений:
- ✅ Максимальный убыток = $100 на $100 позицию
- ✅ Equity всегда >= 0
- ✅ Floating PnL корректно рассчитан
- ✅ Trailing stop работает правильно
- ✅ Реалистичный slippage

---

## 🚀 ПОРЯДОК ВНЕДРЕНИЯ

1. **Сначала**: Добавить `cap_loss_to_margin()` функцию
2. **Затем**: Применить её во всех 4 местах закрытия позиций
3. **После**: Исправить floating PnL (для корректных метрик)
4. **Далее**: Исправить trailing stop logic
5. **В конце**: Добавить slippage для реализма

---

## ✅ КАК ПРОВЕРИТЬ ИСПРАВЛЕНИЯ

### Тест 1: Проверка ограничения убытков
```python
# Создать позицию с большим убытком
# Убедиться что max loss = position_size - commission
position_size = 100
entry_price = 100
close_price = 50  # -50% убыток при 10x leverage = -500%!
# После cap_loss_to_margin(): убыток должен быть ~-$99.94
```

### Тест 2: Проверка floating PnL
```python
# Открыть несколько позиций
# Проверить что sim.calculate_floating_pnl() != 0
# Проверить что min_equity учитывает floating losses
```

### Тест 3: Проверка trailing stop
```python
# Для LONG позиции:
# entry_price = 100
# price движется до 110 (trailing активируется)
# trailing_stop должен быть ~108.5 (110 * 0.985)
# Если price идет до 112, trailing_stop = 110.36
# trailing_stop НЕ должен опускаться обратно!
```

---

## 📝 ИТОГО

**Главная проблема**: Текущая реализация НЕ соблюдает принцип isolated margin - убытки не ограничены размером маржи.

**Решение**: Внедрить `cap_loss_to_margin()` во всех местах закрытия позиций.

**Результат**: Корректная симуляция isolated margin торговли с реалистичными результатами.