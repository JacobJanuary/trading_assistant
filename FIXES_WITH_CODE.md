# 🛠️ ГОТОВЫЕ ИСПРАВЛЕНИЯ С КОДОМ

## FIX #1: Добавление cap_loss_to_margin()

### Файл: `/home/elcrypto/trading_assistant/trading_simulation.py`
**Добавить после строки ~70 (после __init__):**

```python
def cap_loss_to_margin(self, gross_pnl, entry_commission, exit_commission):
    """
    Ограничивает убыток размером изолированной маржи

    При isolated margin максимальный убыток = размер маржи минус entry_commission
    (т.к. entry_commission уже была списана при открытии)

    Args:
        gross_pnl: Валовая прибыль/убыток до комиссий
        entry_commission: Комиссия при входе
        exit_commission: Комиссия при выходе

    Returns:
        float: Чистый PnL, ограниченный размером маржи
    """
    # Общие комиссии
    total_commission = entry_commission + exit_commission

    # Чистый PnL после комиссий
    net_pnl = gross_pnl - total_commission

    # Максимально возможный убыток = маржа минус входная комиссия
    # (входная комиссия уже списана, поэтому она не входит в убыток)
    max_loss = -(self.position_size - entry_commission)

    # Возвращаем большее из двух значений (ограничение убытка)
    return max(net_pnl, max_loss)
```

---

## FIX #2: Применение cap_loss_to_margin() в _simulate_fixed_tp_sl()

### Файл: `/home/elcrypto/trading_assistant/trading_simulation.py`
**Заменить строки 282-291:**

```python
# БЫЛО:
gross_pnl = effective_position * (pnl_percent / 100)
pnl_usd = gross_pnl - total_commission

# ЗАМЕНИТЬ НА:
gross_pnl = effective_position * (pnl_percent / 100)

# Применяем ограничение isolated margin
pnl_usd = self.cap_loss_to_margin(gross_pnl, entry_commission, exit_commission)

# Логирование для отладки (опционально)
if pnl_usd < gross_pnl - total_commission:
    print(f"[CAP APPLIED] Original loss: {gross_pnl - total_commission:.2f}, "
          f"Capped to: {pnl_usd:.2f}")
```

---

## FIX #3: Исправление calculate_trailing_stop_exit() в database.py

### Файл: `/home/elcrypto/trading_assistant/database.py`
**Заменить строки ~1607-1612:**

```python
# БЫЛО:
gross_pnl = effective_position * (pnl_percent / 100)
net_pnl = gross_pnl - total_commission

# ЗАМЕНИТЬ НА:
gross_pnl = effective_position * (pnl_percent / 100)
net_pnl = gross_pnl - total_commission

# Применяем ограничение isolated margin
max_loss = -(position_size - entry_commission)
if net_pnl < max_loss:
    print(f"[TRAILING CAP] Capping loss from {net_pnl:.2f} to {max_loss:.2f}")
    net_pnl = max_loss
```

---

## FIX #4: Исправление Trailing Stop Direction

### Файл: `/home/elcrypto/trading_assistant/database.py`
**Заменить строки 1573-1580:**

```python
# БЫЛО (НЕПРАВИЛЬНО):
if is_long:
    if new_stop > trailing_stop_price:
        trailing_stop_price = new_stop
        print(f"  [TRAILING] Updated trailing stop to {trailing_stop_price:.4f}")
else:
    if new_stop < trailing_stop_price:
        trailing_stop_price = new_stop
        print(f"  [TRAILING] Updated trailing stop to {trailing_stop_price:.4f}")

# ЗАМЕНИТЬ НА (ПРАВИЛЬНО):
if is_long:
    # Для LONG: trailing stop может только ПОВЫШАТЬСЯ
    # (защищает прибыль при росте цены)
    if trailing_stop_price is None or new_stop > trailing_stop_price:
        trailing_stop_price = new_stop
        print(f"  [TRAILING LONG] Stop raised to {trailing_stop_price:.4f}")
else:
    # Для SHORT: trailing stop может только ПОНИЖАТЬСЯ
    # (защищает прибыль при падении цены)
    if trailing_stop_price is None or new_stop < trailing_stop_price:
        trailing_stop_price = new_stop
        print(f"  [TRAILING SHORT] Stop lowered to {trailing_stop_price:.4f}")
```

---

## FIX #5: Исправление force_close_all_positions()

### Файл: `/home/elcrypto/trading_assistant/trading_simulation.py`
**Заменить строки ~445-455:**

```python
# БЫЛО:
gross_pnl = effective_position * (pnl_percent / 100)
net_pnl = gross_pnl - total_commission
self.available_capital += position['position_size'] + net_pnl

# ЗАМЕНИТЬ НА:
gross_pnl = effective_position * (pnl_percent / 100)

# Применяем ограничение isolated margin при принудительном закрытии
capped_pnl = self.cap_loss_to_margin(gross_pnl, entry_commission, exit_commission)

# Возвращаем капитал
self.available_capital += position['position_size'] + capped_pnl

# Логирование
if capped_pnl != gross_pnl - total_commission:
    print(f"[FORCE CLOSE CAP] Position {pair}: "
          f"Original PnL: {gross_pnl - total_commission:.2f}, "
          f"Capped to: {capped_pnl:.2f}")
```

---

## FIX #6: Исправление close_due_positions()

### Файл: `/home/elcrypto/trading_assistant/trading_simulation.py`
**Заменить строки ~385-395:**

```python
# БЫЛО:
if 'pnl_usd' in position and position['pnl_usd'] is not None:
    self.total_pnl += position['pnl_usd']
    self.available_capital += position['position_size'] + position['pnl_usd']

# ЗАМЕНИТЬ НА:
if 'pnl_usd' in position and position['pnl_usd'] is not None:
    # PnL уже должен быть ограничен, но проверяем на всякий случай
    entry_commission = position.get('entry_commission',
                                   position['position_size'] * self.leverage * 0.0006)
    exit_commission = position['position_size'] * self.leverage * 0.0006

    # Дополнительная проверка ограничения
    max_loss = -(position['position_size'] - entry_commission)
    actual_pnl = max(position['pnl_usd'], max_loss)

    if actual_pnl != position['pnl_usd']:
        print(f"[CLOSE DUE CAP] Adjusting PnL from {position['pnl_usd']:.2f} "
              f"to {actual_pnl:.2f}")

    self.total_pnl += actual_pnl
    self.available_capital += position['position_size'] + actual_pnl
```

---

## FIX #7: Исправление Floating PnL

### Файл: `/home/elcrypto/trading_assistant/database.py`
**Заменить строку 2947:**

```python
# БЫЛО:
sim.update_equity_metrics(wave_time, market_data_by_pair=None)

# ЗАМЕНИТЬ НА:
# Подготавливаем market_data для расчета floating PnL
market_data_by_pair = {}
for pair, position in sim.open_positions.items():
    # Получаем market_data из кэша
    signal_id = position.get('signal_id')
    if signal_id and signal_id in market_data_cache:
        market_data_by_pair[pair] = market_data_cache[signal_id]

# Теперь передаем реальные данные для расчета floating PnL
sim.update_equity_metrics(wave_time, market_data_by_pair)

print(f"[EQUITY UPDATE] Positions: {len(sim.open_positions)}, "
      f"Market data: {len(market_data_by_pair)}")
```

### Файл: `/home/elcrypto/trading_assistant/trading_simulation.py`
**В методе calculate_floating_pnl() добавить ограничение (строка ~350):**

```python
# После расчета floating_pnl для каждой позиции:
floating_pnl = effective_position * (pnl_percent / 100)

# Ограничиваем floating убыток 95% маржи
# (5% резерв так как позиция еще не закрыта)
max_floating_loss = -position_size * 0.95
if floating_pnl < max_floating_loss:
    print(f"[FLOATING CAP] {pair}: Capping from {floating_pnl:.2f} "
          f"to {max_floating_loss:.2f}")
    floating_pnl = max_floating_loss
```

---

## FIX #8: Добавление Slippage

### В начале файлов добавить константу:

```python
# trading_simulation.py и database.py
SLIPPAGE_PERCENT = 0.05  # 0.05% проскальзывание на stop-loss
```

### Файл: `/home/elcrypto/trading_assistant/trading_simulation.py`
**В _simulate_fixed_tp_sl(), строки ~252-254 и 262-264:**

```python
# БЫЛО:
if is_long and low <= sl_price:
    result['close_price'] = sl_price
    result['close_reason'] = 'stop_loss'

# ЗАМЕНИТЬ НА:
if is_long and low <= sl_price:
    # Применяем slippage (исполнение хуже на 0.05%)
    result['close_price'] = sl_price * (1 - SLIPPAGE_PERCENT / 100)
    result['close_reason'] = 'stop_loss'
    print(f"[SLIPPAGE] SL at {sl_price:.4f}, executed at {result['close_price']:.4f}")
```

### Файл: `/home/elcrypto/trading_assistant/database.py`
**В calculate_trailing_stop_exit(), строка ~1530:**

```python
# БЫЛО:
close_price = sl_price

# ЗАМЕНИТЬ НА:
# Применяем slippage на stop-loss
slippage = 0.05 / 100  # 0.05%
if is_long:
    close_price = sl_price * (1 - slippage)
else:
    close_price = sl_price * (1 + slippage)
print(f"[SLIPPAGE] SL: {sl_price:.4f} -> Executed: {close_price:.4f}")
```

---

## 🧪 ТЕСТОВЫЙ КОД ДЛЯ ПРОВЕРКИ

```python
# test_isolated_margin.py

def test_cap_loss_to_margin():
    """Тест ограничения убытков"""
    from trading_simulation import TradingSimulation

    sim = TradingSimulation(
        initial_capital=1000,
        position_size=100,
        leverage=10,
        tp_percent=2,
        sl_percent=1
    )

    # Тест 1: Большой убыток
    gross_pnl = -500  # -50% при 10x = -500%
    entry_comm = 0.6  # 0.06% от 1000
    exit_comm = 0.6

    capped = sim.cap_loss_to_margin(gross_pnl, entry_comm, exit_comm)

    print(f"Gross PnL: {gross_pnl}")
    print(f"Capped PnL: {capped}")
    print(f"Max expected loss: {-(100 - 0.6)}")

    assert capped >= -(100 - 0.6), "Loss exceeds margin!"
    assert capped <= 0, "Loss should be negative!"

    print("✅ Test passed: Loss properly capped to margin")

if __name__ == "__main__":
    test_cap_loss_to_margin()
```

---

## 📋 ЧЕКЛИСТ ВНЕДРЕНИЯ

- [ ] Добавить функцию `cap_loss_to_margin()` в `trading_simulation.py`
- [ ] Применить cap в `_simulate_fixed_tp_sl()`
- [ ] Применить cap в `calculate_trailing_stop_exit()`
- [ ] Исправить направление trailing stop
- [ ] Применить cap в `force_close_all_positions()`
- [ ] Применить cap в `close_due_positions()`
- [ ] Исправить floating PnL расчет
- [ ] Добавить slippage на stop-loss
- [ ] Запустить тесты
- [ ] Проверить что убытки не превышают маржу

---

## ⚠️ ВАЖНЫЕ МОМЕНТЫ

1. **Entry commission** уже списана при открытии, поэтому max_loss = -(margin - entry_commission)

2. **Floating PnL** ограничиваем 95% маржи (не 100%), т.к. позиция еще открыта

3. **Slippage** применяется только к stop-loss, не к take-profit

4. **Trailing stop** для LONG может только расти, для SHORT только падать

5. **Логирование** добавлено для отладки - можно убрать в продакшене