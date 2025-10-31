# CRITICAL BUGS IN CURRENT IMPLEMENTATION - WITH FIXES

## Overview
This document lists 6 critical bugs found when comparing CURRENT implementation against the REFERENCE implementation from backtest_both.py. Each bug has:
1. Location (file + line)
2. Current code (WRONG)
3. Impact
4. Fixed code (CORRECT)

---

## BUG #1: Trailing Stop Ratcheting Moves UP Instead of DOWN

### Location
- **File**: `/home/elcrypto/trading_assistant/database.py`
- **Function**: `calculate_trailing_stop_exit()`
- **Lines**: 1575-1577 (LONG position logic)

### Current Code (WRONG)
```python
if is_trailing_active:
    new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
    if new_stop > trailing_stop_price:  # <-- BUG: Allows stop to go UP
        trailing_stop_price = new_stop
```

### Problem
- For LONG positions, the trailing stop should ONLY move DOWN (tighten), never up
- Current code checks `if new_stop > trailing_stop_price`, which allows UP movement
- Example: If trailing_stop_price=100 and new_stop=105, it sets trailing_stop_price=105 (goes UP!)
- This violates trailing stop semantics - it can let losing trades run further down

### Impact
- **Severity**: CRITICAL
- Positions exit at worse prices than intended
- Losses can exceed expectations
- Back-test results are incorrect

### Fixed Code
```python
if is_trailing_active:
    new_stop = best_price_for_trailing * (1 - trailing_distance_pct / 100)
    if new_stop < trailing_stop_price:  # CORRECT: Only allows DOWN movement
        trailing_stop_price = new_stop
```

### Also applies to SHORT logic (line 1551-1552)
```python
# CURRENT (WRONG)
if new_stop < trailing_stop_price:
    trailing_stop_price = new_stop

# SHOULD BE (for SHORT)
if new_stop > trailing_stop_price:  # For SHORT, > means UP (worse for short)
    trailing_stop_price = new_stop
```

---

## BUG #2: Floating PnL NOT Calculated - Always Zero

### Location
- **File**: `/home/elcrypto/trading_assistant/database.py`
- **Function**: `process_scoring_signals_batch_v2()`
- **Line**: 2947

### Current Code (WRONG)
```python
# 2. Обновляем метрики equity (с учетом floating PnL открытых позиций)
sim.update_equity_metrics(wave_time, market_data_by_pair=None)  # <-- BUG: market_data_by_pair=None!
```

### Problem
- `update_equity_metrics()` needs `market_data_by_pair` to calculate current prices
- Passing `None` means floating_pnl is always 0
- Result: equity calculation is wrong, doesn't reflect actual floating losses
- min_equity tracking is incorrect

### Impact
- **Severity**: CRITICAL
- Equity metrics are wrong
- Min equity is understated (doesn't account for floating losses)
- Max drawdown is understated
- Back-test results are too optimistic

### Fixed Code
```python
# Build market_data_by_pair from open positions
market_data_by_pair = {}
for pair, position in sim.open_positions.items():
    signal_id = position['signal_id']
    if signal_id in market_data_cached:
        market_data_by_pair[pair] = market_data_cached[signal_id]

# 2. Обновляем метрики equity (с учетом floating PnL открытых позиций)
sim.update_equity_metrics(wave_time, market_data_by_pair=market_data_by_pair)
```

---

## BUG #3: Missing Loss Capping in Fixed TP/SL Simulation

### Location
- **File**: `/home/elcrypto/trading_assistant/trading_simulation.py`
- **Function**: `_simulate_fixed_tp_sl()`
- **Lines**: 282-291

### Current Code (WRONG)
```python
# Расчет PnL
pnl_usd = 0
if is_closed:
    if is_short:
        pnl_percent = ((entry_price - close_price) / entry_price) * 100
    else:
        pnl_percent = ((close_price - entry_price) / entry_price) * 100

    gross_pnl = effective_position * (pnl_percent / 100)
    pnl_usd = gross_pnl - total_commission  # <-- NO CAPPING!
```

### Problem
- Reference implementation uses `cap_loss_to_margin()` to limit losses
- Without capping, losses can exceed position_margin (isolated margin violation!)
- Example: position_size=$100, leverage=10, loss can be > $100

### Impact
- **Severity**: CRITICAL
- Account margin rules violated
- Back-test results don't match real trading behavior
- Can report negative equity

### Fixed Code
```python
# Расчет PnL
pnl_usd = 0
if is_closed:
    if is_short:
        pnl_percent = ((entry_price - close_price) / entry_price) * 100
    else:
        pnl_percent = ((close_price - entry_price) / entry_price) * 100

    gross_pnl = effective_position * (pnl_percent / 100)
    
    # CAP LOSS TO MARGIN (isolated margin rules)
    max_loss = -(self.position_size - entry_commission)
    net_pnl = gross_pnl - total_commission
    pnl_usd = max(net_pnl, max_loss)  # CAPPED!
```

---

## BUG #4: No Slippage on Stop Loss Execution

### Location
- **Files**: Multiple
  - `/home/elcrypto/trading_assistant/trading_simulation.py` (lines 252-254, 262-264)
  - `/home/elcrypto/trading_assistant/database.py` (lines 1528-1531)

### Current Code (WRONG)
```python
# In trading_simulation.py
elif low_price <= sl_price:
    is_closed = True
    close_reason = 'stop_loss'
    close_price = sl_price  # <-- Exact price, no slippage
    close_time = current_time
```

### Problem
- Reference implementation applies slippage (0.05%) to make execution realistic
- Current code executes at exact SL price
- In real trading, stop loss orders often get filled at worse prices
- Back-test results are too optimistic

### Impact
- **Severity**: HIGH
- Unrealistic P&L (better than real-world)
- Back-test results are overstated
- Doesn't account for market impact

### Fixed Code
```python
elif low_price <= sl_price:
    is_closed = True
    close_reason = 'stop_loss'
    
    # Apply slippage (0.05%)
    slippage = getattr(Config, 'DEFAULT_SLIPPAGE_PERCENT', 0.05) / 100
    if is_long:
        close_price = sl_price * (1 - slippage)  # Worse price for long
    else:
        close_price = sl_price * (1 + slippage)  # Worse price for short
    
    close_time = current_time
```

---

## BUG #5: force_close_all_positions Doesn't Cap Loss

### Location
- **File**: `/home/elcrypto/trading_assistant/trading_simulation.py`
- **Function**: `force_close_all_positions()`
- **Line**: 451

### Current Code (WRONG)
```python
if last_price and entry_price > 0:
    # Пересчитываем PnL
    effective_position = self.position_size * self.leverage
    
    if is_long:
        pnl_percent = ((last_price - entry_price) / entry_price) * 100
    else:
        pnl_percent = ((entry_price - last_price) / entry_price) * 100
    
    gross_pnl = effective_position * (pnl_percent / 100)
    
    # Комиссии
    commission_rate = Config.DEFAULT_COMMISSION_RATE
    entry_commission = effective_position * commission_rate
    exit_commission = effective_position * commission_rate
    total_commission = entry_commission + exit_commission
    
    net_pnl = gross_pnl - total_commission  # <-- NO CAPPING!
```

### Problem
- When positions are force-closed at period end, loss is NOT capped to margin
- Reference implementation checks: `if pnl_percent < -max_loss_percent: ...cap the price...`
- Without capping, losses can exceed position_margin

### Impact
- **Severity**: CRITICAL
- Period-end closures can have losses > margin
- Account equity can go negative
- Conflicts with isolated margin rules

### Fixed Code
```python
if last_price and entry_price > 0:
    # Пересчитываем PnL
    effective_position = self.position_size * self.leverage
    
    if is_long:
        pnl_percent = ((last_price - entry_price) / entry_price) * 100
    else:
        pnl_percent = ((entry_price - last_price) / entry_price) * 100
    
    # CAP TO 95% OF MARGIN (like reference)
    max_loss_percent = (self.position_size * 0.95) / effective_position * 100
    
    if pnl_percent < -max_loss_percent:
        if is_long:
            last_price = entry_price * (1 - max_loss_percent / 100)
        else:
            last_price = entry_price * (1 + max_loss_percent / 100)
        close_reason = 'forced_liquidation'
    else:
        close_reason = 'period_end'
    
    gross_pnl = effective_position * (pnl_percent / 100)
    
    # Комиссии
    commission_rate = Config.DEFAULT_COMMISSION_RATE
    entry_commission = effective_position * commission_rate
    exit_commission = effective_position * commission_rate
    total_commission = entry_commission + exit_commission
    
    net_pnl = gross_pnl - total_commission
    
    # FINAL CAP
    max_loss = -(self.position_size - entry_commission)
    net_pnl = max(net_pnl, max_loss)
```

---

## BUG #6: No Signal Filtering Validation in Main Function

### Location
- **File**: `/home/elcrypto/trading_assistant/database.py`
- **Function**: `process_scoring_signals_batch_v2()`
- **Lines**: 3259, 3309

### Current Code (WRONG)
```python
# Line 3259
all_signals = get_scoring_signals(db, date_filter, score_week_min, score_month_min, allowed_hours)

# Later at line 3309 - only sorts by score_week!
sorted_signals = sorted(signals, key=lambda x: x.get('score_week', 0), reverse=True)

# No explicit check that score_month >= score_month_min
```

### Problem
- Reference implementation has explicit filtering:
  ```python
  filtered_signals = [
      s for s in all_signals_cached
      if s['score_week'] >= score_w and s['score_month'] >= score_m
  ]
  ```
- Current code assumes filtering happens in `get_scoring_signals()`
- If `get_scoring_signals()` has a bug, invalid signals slip through
- Only sorting by score_week doesn't ensure score_month minimum

### Impact
- **Severity**: MEDIUM
- Low-score_month signals might be traded
- Results don't match reference implementation
- Filtering logic is implicit, hard to verify

### Fixed Code
```python
# In process_scoring_signals_batch_v2, after loading signals

# Step 1: Explicit filtering (like reference)
filtered_signals = [
    s for s in all_signals
    if s.get('score_week', 0) >= score_week_min and s.get('score_month', 0) >= score_month_min
]

if not filtered_signals:
    print(f"[SCORING V2] No signals pass score filters: score_week>={score_week_min}, score_month>={score_month_min}")
    return {'processed': 0, 'errors': 0, 'simulation_summary': {}, 'stats': {}}

print(f"[SCORING V2] After score filtering: {len(filtered_signals)} signals (from {len(all_signals)})")

# Step 2: Group by waves (rest of function uses filtered_signals)
signals_by_wave = group_signals_by_wave(filtered_signals, wave_interval)
```

---

## Summary of Fixes Priority

| Bug # | Severity | Fix Effort | Impact on Results |
|-------|----------|-----------|------------------|
| #1 | CRITICAL | 2 lines | Trailing stop behaves incorrectly |
| #2 | CRITICAL | 10 lines | Equity/drawdown metrics wrong |
| #3 | CRITICAL | 5 lines | Losses exceed margin |
| #4 | HIGH | 5 lines | P&L too optimistic |
| #5 | CRITICAL | 20 lines | Period-end losses exceed margin |
| #6 | MEDIUM | 5 lines | May allow invalid signals |

### Recommended Fix Order
1. **Bug #2** (Floating PnL) - Affects all equity calculations
2. **Bug #3** (Loss capping in _simulate_fixed_tp_sl) - Fixes margin violations
3. **Bug #5** (Loss capping in force_close_all_positions) - Fixes period-end margin violations
4. **Bug #1** (Trailing stop ratcheting) - Fixes exit logic
5. **Bug #4** (Slippage) - Adds realism
6. **Bug #6** (Signal filtering) - Adds validation

---

## Testing After Fixes

After applying fixes:

1. **Re-run backtest** with same parameters as reference
2. **Compare equity curves** - should be very close
3. **Compare PnL distributions** - similar win/loss ratios
4. **Check for negative equity** - should never happen
5. **Validate losses < margin** - all trades should comply

