# SIGNAL PROCESSING COMPARISON - EXECUTIVE SUMMARY

## Quick Overview

Comprehensive comparison of signal processing logic between:
- **REFERENCE**: `/home/elcrypto/calk_wk/backtest_both.py` (correct, working implementation)
- **CURRENT**: `/home/elcrypto/trading_assistant/` (has bugs, needs fixes)

**Result**: CURRENT implementation has **6 CRITICAL BUGS** that make back-test results incorrect.

---

## Critical Bugs Found

| # | Bug | File | Line(s) | Severity | Impact |
|---|-----|------|---------|----------|--------|
| 1 | Trailing Stop Ratchets UP | database.py | 1575-1577 | CRITICAL | Wrong exit prices |
| 2 | Floating PnL Always Zero | database.py | 2947 | CRITICAL | Wrong equity metrics |
| 3 | No Loss Capping (Fixed TP/SL) | trading_simulation.py | 282-291 | CRITICAL | Losses exceed margin |
| 4 | No Slippage on Stop Loss | trading_simulation.py | 252-254, 262-264 | HIGH | Too optimistic P&L |
| 5 | No Loss Capping (Period End) | trading_simulation.py | 451 | CRITICAL | Losses exceed margin |
| 6 | No Signal Filtering Check | database.py | 3259, 3309 | MEDIUM | May allow invalid signals |

---

## What Each Bug Means

### Bug #1: Trailing Stop Ratchets UP Instead of DOWN
```
Impact: Position exits at WORSE prices (more loss) than intended
Example: For Long position, trailing_stop_price goes from 100 to 105 (UP!)
Should: Trailing_stop_price should only go DOWN (to 99, 98, 97...)
```

### Bug #2: Floating PnL NOT Calculated
```
Impact: Equity calculations are WRONG and TOO OPTIMISTIC
Current: Passes market_data_by_pair=None to update_equity_metrics()
Result: floating_pnl is always 0, min_equity is understated
Actual: Open positions with floating losses aren't reflected in equity
```

### Bug #3: No Loss Capping in _simulate_fixed_tp_sl()
```
Impact: Losses can EXCEED account margin (isolated margin violation)
Example: $100 position with 10x leverage can lose > $100
Should: Max loss = margin - entry_commission = ~$99.94
```

### Bug #4: No Slippage on Stop Loss
```
Impact: Back-test results are TOO OPTIMISTIC (better than real trading)
Example: Stop loss at $100 executed at exactly $100 (unrealistic)
Real: Usually fills at $99.95-$99.90 (slippage 0.05%)
```

### Bug #5: force_close_all_positions Doesn't Cap Loss
```
Impact: Period-end position closures can have losses > margin
When: At end of simulation, positions are forced closed
Issue: No check if loss exceeds 95% of margin
Result: Account equity can go negative
```

### Bug #6: No Explicit Signal Filtering
```
Impact: Invalid signals (low score_month) might be traded
Current: Assumes get_scoring_signals() filters correctly
Missing: No validation that score_month >= score_month_min
Risk: If bug in get_scoring_signals(), invalid signals slip through
```

---

## Key Differences Summary

### Signal Filtering
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Filtering | Explicit in main loop | Delegated to function |
| Validation | 2-check (week AND month) | Assumed (only 1 check shown) |
| Safety | Explicit list comprehension | Implicit in get_scoring_signals |

### Position Management
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Wave processing | Sequential, sorted | Sequential via TradingSimulation |
| Duplicate check | Checks close_time > wave_time | Only checks existence |
| Floating PnL | Fully calculated | Always 0 (bug) |
| Min equity | Accurate with floating losses | Wrong (doesn't use floating PnL) |

### Trailing Stop
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Ratcheting logic | max() for DOWN movement | if new_stop > trailing_stop_price |
| Result for LONG | Only moves DOWN | Can move UP (BUG!) |
| Correctness | Correct semantics | Violates semantics |

### Stop Loss & Liquidation
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Slippage | Applied (0.05%) | NOT applied |
| Liquidation capping | Yes (margin - entry_commission) | No capping |
| Priority | Liquidation > SL > TS | Same but no capping |

### PnL Calculation
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| cap_loss_to_margin() | Implemented, used in 3 places | MISSING |
| Loss limit | Enforced | Not enforced |
| Margin compliance | Yes | No (CAN VIOLATE) |
| Period-end closure | Capped to 95% margin | No cap |

### Capital Management
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Available capital | Deducted before, returned on close | Same logic |
| Floating PnL | Included in equity | Always 0 (bug) |
| Min equity tracking | With floating losses | Without floating losses |
| Accuracy | High | Low |

### Commission Handling
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Entry & Exit | Both calculated | Both calculated |
| Rate | 0.0006 (0.06%) | Same |
| Capping | Considered in loss cap | Not enforced |

### Simulation End Time
| Aspect | REFERENCE | CURRENT |
|--------|-----------|---------|
| Calculation | end_date (buffer-adjusted) | last_signal + 48 hours |
| Timeframe | Same period for all backtests | Different per backtest |
| Impact | Consistent results | Varies with signal timing |

---

## Implementation Architecture Differences

### REFERENCE (backtest_both.py)
```
run_backtest()
├── Load signals (with min_score filtering)
├── Load market data
├── For each parameter combination:
│   ├── Filter signals by score_week AND score_month
│   ├── Group into 15-min waves
│   ├── For each wave (sequential):
│   │   ├── Close due positions (explicit check)
│   │   ├── Calculate floating PnL (with market data)
│   │   ├── For each signal (sorted by score_week):
│   │   │   ├── Check capital available
│   │   │   ├── Check not duplicate (close_time check)
│   │   │   ├── Simulate position (with cap_loss_to_margin)
│   │   │   └── Track stats
│   │   └── Update min_equity (with floating PnL)
│   └── Close remaining positions (with capping)
└── Store results
```

### CURRENT (database.py + trading_simulation.py)
```
process_scoring_signals_batch_v2()
├── Load signals (assumes filtering)
├── Create TradingSimulation object
├── Group into waves
├── For each wave:
│   ├── Call sim.close_due_positions()  [CORRECT]
│   ├── Call sim.update_equity_metrics(market_data=None)  [BUG: None!]
│   ├── For each signal (sorted by score_week):
│   │   ├── Call sim.open_position()
│   │   │   ├── Check capital + duplicate [PARTIAL BUG]
│   │   │   ├── Simulate via calculate_trailing_stop_exit()
│   │   │   │   └── [BUG: Wrong trailing logic]
│   │   │   │   └── [BUG: No slippage]
│   │   │   │   └── [BUG: No loss capping]
│   │   │   └── Add to open_positions
│   │   └── Track stats
│   └── Min equity calculation [WRONG: no floating PnL]
└── Call force_close_all_positions()  [BUG: No capping]
```

---

## Priority Action Items

### HIGH PRIORITY (affects core logic)
1. **Fix Floating PnL calculation** - affects all equity metrics
2. **Add loss capping in _simulate_fixed_tp_sl()** - prevents margin violations
3. **Add loss capping in force_close_all_positions()** - prevents period-end violations

### MEDIUM PRIORITY (affects quality)
4. **Fix trailing stop ratcheting** - correct exit logic
5. **Add slippage to stop loss** - add realism

### LOW PRIORITY (affects validation)
6. **Add explicit signal filtering** - validation

---

## Files to Modify

```
/home/elcrypto/trading_assistant/
├── database.py
│   ├── calculate_trailing_stop_exit() [BUG #1, #4]
│   └── process_scoring_signals_batch_v2() [BUG #2, #6]
├── trading_simulation.py
│   ├── _simulate_fixed_tp_sl() [BUG #3, #4]
│   └── force_close_all_positions() [BUG #5]
└── IMPLEMENTATION_COMPARISON.md [REFERENCE]
```

---

## Verification Checklist

After fixes are applied:

- [ ] Trailing stop only moves DOWN for LONG, only moves UP for SHORT
- [ ] Floating PnL calculation uses actual market data
- [ ] All losses capped to margin - entry_commission
- [ ] All period-end closures cap loss to 95% of margin
- [ ] Stop loss orders have 0.05% slippage
- [ ] No negative equity values in results
- [ ] Min equity matches reference implementation
- [ ] Equity curve shape similar to reference
- [ ] Win/loss ratios similar to reference
- [ ] Max drawdown calculation includes floating losses

---

## Testing Strategy

1. **Unit tests**: Each fix independently
2. **Integration test**: Full backtest run
3. **Regression test**: Compare against reference results
4. **Edge cases**: Liquidation events, period-end closures

---

## References

- **Detailed Analysis**: See `IMPLEMENTATION_COMPARISON.md`
- **Fixes with Code**: See `BUGS_AND_FIXES.md`
- **Reference Code**: `/home/elcrypto/calk_wk/backtest_both.py`

