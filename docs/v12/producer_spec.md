# Premarket Report V1.2 Producer Spec

This is the fachliche source of truth for the current V1.2 producer as implemented in `tools/producer/premarket-scanner.py`.

Important distinction: V1.2 is not a discretionary text template. It is a deterministic producer that emits a Telegram report from Polygon snapshot data, Polygon reference market caps, Barchart GEX levels, and a local GEX-history store. The report text is only the final rendering layer.

## Purpose

The report builds a small US-equities premarket trade desk for the final 09:00-09:30 ET window:

- identify liquid, large-cap premarket movers;
- rank them by simple momentum quality and tradeability;
- assign a side bias before the open;
- choose one trigger level per candidate;
- force discipline through close-through entries, warnings, and an audit trail;
- overlay Barchart Gamma Exposure context so continuation/fade setups are not read without index and dealer-positioning context.

The report is a watchlist and execution context, not an automatic order system.

## Engine

Engine name:

```text
Gapfade Direction + Fib Trigger + Close-through Entry
```

Meaning:

- `Gapfade Direction`: direction starts from gap direction, then selected low-RVOL gap-up names can be flipped into a short fade via GapFade1.
- `Fib Trigger`: each candidate gets a trigger from the computed range Fib ladder, with PM high/low fallback.
- `Close-through Entry`: a trigger touch is not enough. Entry is valid only after the first 1-minute candle closes through the trigger in the expected direction.

Producer constants:

```text
direction_policy = gapfade
trigger_policy   = fib
entry_policy     = close_through
report_template  = premarket_v1_2_a_watchlist_avoid
```

## Producer Flow

1. `premarket-scan-check.py --with-gex`
2. `premarket-propose.py --with-gex`
3. `premarket-scanner.py --meta-json --with-gex`
4. `premarket-verify.py`
5. `premarket-apply.py`

The proposal/verify/apply wrapper fails closed. If any gate fails, the wrapper emits `NO_REPLY` instead of sending a stale or malformed report.

Verification gates:

- current phase must be `premarket_final`, defined as 09:00 <= ET < 09:30;
- proposal age must be <= 300 seconds;
- scanner exit code must be 0;
- scanner payload `ok` must be true;
- `has_fetch_errors` must be false;
- `has_stale_flags` must be false;
- rendered message must be non-empty.

## Report Structure

Header:

- report title and UTC timestamp;
- engine label;
- candidate count;
- EOD-validated / close-through entry statement;
- optional `Market Read` line from Barchart index flip context.

Sections:

- `A-Setups / Tradeable nur nach Trigger`
- `Watchlist / Nur bei sauberem Trigger`
- `Avoid / Kein First-Choice-Trade`
- `Alle Scanner-Kandidaten / Audit Trail`
- optional `GapFade1 - Gap Up Fade Candidates`
- fixed entry, exit, and risk footer

## Category Definitions

### A-Setups

Included when:

- `tradeability_grade` is `A` or `B+`;
- `trigger_price` exists;
- candidate is among the top 4 after ranking.

Ranking key:

```text
grade_rank desc, scanner_score desc, warning_count asc
```

Grade rank:

```text
A = 4
B+ = 3
B = 2
C = 1
```

The label says `A-Setups`, but V1.2 intentionally allows `B+` in this section. That is why a line can read `B+ Short Continuation` inside the A-Setups section.

### Watchlist

Included when:

- not already used in A-Setups;
- `tradeability_grade` is `B`;
- candidate is among the first 4 matching names after ranking.

### Avoid

Included when:

- not already used in A-Setups or Watchlist;
- candidate is part of the top scanner candidate set;
- candidate is among the first 4 remaining names after ranking.

Avoid reason construction:

- include all warning flags;
- add `low_tradeability` if grade is `C`;
- if no reason exists, use `weaker_than_top_setups`.

### Audit Trail

Includes every final scanner candidate, not only tradeable names:

```text
ticker | grade | side | scanner score | trigger
```

This is deliberately redundant. It makes the rendered report auditable against the structured producer payload.

## Candidate Inputs

Required Polygon snapshot fields per symbol:

- `ticker`
- `min.c` as current/premarket price
- `min.av` as snapshot accumulated volume
- `min.h` as V1.2 range high
- `min.l` as V1.2 range low
- `prevDay.c` previous close
- `prevDay.h` previous day high
- `prevDay.l` previous day low
- `prevDay.v` previous day volume

Required Polygon reference field:

- `results.market_cap`

Required Barchart GEX fields for each overlaid ticker:

- `spot_price`
- `gamma_flip`
- `call_wall`
- `put_wall`
- `iv_rank`
- `max_call_oi_strike`
- `max_put_oi_strike`
- `near_support_levels`
- `near_resistance_levels`
- signed GEX by strike

Required local GEX-history fields for index market read:

- ticker in `SPY`, `QQQ`, `SMH`, `IWM`;
- current trading day;
- preferred phase `open`;
- `spot_price`;
- `gamma_flip`.

## Data Sources

Primary runtime data:

- Polygon `/v2/snapshot/locale/us/markets/stocks/tickers`
- Polygon `/v3/reference/tickers/{ticker}`
- Barchart `/stocks/quotes/{TICKER}/gamma-exposure`
- local SQLite `state/barchart_levels.sqlite`

Secret inputs:

- Polygon key from `sync/polygon-api-key.txt` in the original workspace implementation.
- This repo ships `.env.example`; do not commit real keys.

## Candidate Admission Filters

Hard filters before scoring:

```text
previous_close >= 10.00
pm_price >= 10.00
pm_vol >= 500000
abs(pm_change_pct) >= 3.0
market_cap >= 2000000000
market_cap is present
```

Computed fields:

```text
pm_change_pct = (pm_price - prev_close) / prev_close * 100
rel_vol = pm_vol / (prevDay.v / 6.5) if prevDay.v > 0 else 0
market_cap_b = market_cap / 1e9 rounded to 1 decimal
```

No hard admission filter currently exists for:

- spread;
- float;
- options liquidity;
- Barchart GEX availability;
- narrow PM range;
- low relative volume beyond the absolute 500k volume gate.

Those are warnings/category inputs, not candidate admission gates. If GEX fails, the report can still be scanner-only unless the wrapper marks fetch/stale failure.

## Range And Fib Model

V1.2 exact parity range:

```text
range_high = min.h
range_low  = min.l
range      = max(range_high - range_low, 0.01)
```

Levels:

```text
PM_High   = range_high
PM_Low    = range_low
PDH       = prevDay.h
PDL       = prevDay.l
PDC       = prevDay.c
Ext_-27.2 = range_low  - 0.272 * range
Ext_127.2 = range_high + 0.272 * range
Fib_78.6  = range_low  + 0.786 * range
Fib_50.0  = range_low  + 0.500 * range
Fib_23.6  = range_low  + 0.236 * range
```

Quality caveat:

The original code calls these PM high/low, but uses Polygon snapshot `min.h/min.l`. If Polygon returns only the current minute aggregate there, Fib triggers will be too close to current price. Exact V1.2 parity keeps this behavior. Production V1.3 should compute a true 04:00-09:30 ET premarket range from aggregate bars.

## Bias And Direction Logic

Default:

```text
if pm_chg_pct > 0: expected_side = long
else: expected_side = short
```

GapFade1 override:

```text
if pm_chg_pct >= 8.0 and rel_vol < 1.5:
    expected_side = short
    setup_type = GapFade1
    tradeability_read = GapFade Short
```

The override applies only to gap-up names. Negative gaps do not become long fades in V1.2.

Market/GEX context can warn, box, or influence the rendered context line, but the current V1.2 category grade is still computed from scanner score plus warning penalties. The unused helper `derive_tradeability()` contains a richer GEX-aware grading idea, but it is not the active category path in `build_claims()`.

## Trigger Logic

Long trigger:

1. Consider `Fib_78.6`, `Fib_50.0`, `Fib_23.6`.
2. Eligible if `level >= pm_price * 0.995`.
3. Choose the eligible level nearest to `pm_price`.
4. Fallback to `PM_High`.

Short trigger:

1. Consider `Fib_23.6`, `Fib_50.0`, `Fib_78.6`.
2. Eligible if `level <= pm_price * 1.005`.
3. Choose the eligible level nearest to `pm_price`.
4. Fallback to `PM_Low`.

Entry:

```text
long:  first 1m candle close > trigger_price
short: first 1m candle close < trigger_price
```

Touch-only is explicitly invalid.

## Warnings And Flags

Active flags:

- `narrow_pm`: `(pm_high - pm_low) / pm_low * 100 < 0.5`
- `low_rel_vol`: `rel_vol < 0.25`
- `gamma_box`: GEX overlay exists and `gex_overlay[ticker].gamma_box` is present
- `low_tradeability`: avoid-section reason added when grade is `C`
- `weaker_than_top_setups`: avoid-section fallback reason when no explicit warning exists

GapFade1 diagnostic flags:

- `Gap/ADR {x}` based on `abs(pm_price - prev_close) / adr14`
- `PM fading` if price is below 95% of the gap extension
- `PM pullback {n}%` if pullback from high is > 5% of the gap
- `PM near highs`
- `PM Vol >1M`
- `PM Vol >500k`
- weekday flag `Thursday`, `Tue`, or `Wed`
- `RVOL {x}x (very low)` when `rel_vol < 0.7`
- `RVOL {x}x (low)` when `rel_vol < 1.0`
- `Gap {x}% (sweetspot)` when `8 <= gap_pct <= 15`
- `Gap {x}% (very large)` when `gap_pct > 20`

## Market Read Engine

For each index ticker in `SPY`, `QQQ`, `SMH`, `IWM`:

1. Load the latest same-day `open` baseline from `state/barchart_levels.sqlite`.
2. Skip ticker if no baseline, no flip, or no spot.
3. Mark `above flip` when `spot > gamma_flip`.
4. Mark `below flip` otherwise.

Aggregation:

```text
if at least 3 indexes are above flip: bullish
elif at least 3 indexes are below flip: bearish
else: mixed
```

Rendered examples:

```text
Index flip bias bullish (SPY above flip, QQQ above flip, SMH above flip, IWM below flip).
Index flip bias bearish (...).
Index flip bias mixed (...).
```

If no usable baselines exist:

```text
No index-flip baselines available for today.
```

## Historical References

This repo includes real sanitized V1.2 producer snapshots in `docs/v12/examples/` for:

- 2026-05-27
- 2026-05-28
- 2026-05-29
- 2026-06-01
- 2026-06-02
- 2026-06-03
- 2026-06-04

Each day has:

- `report-YYYY-MM-DD.json`: structured producer payload;
- `report-YYYY-MM-DD.md`: rendered Telegram markdown.

The JSON examples keep real candidates, scores, claims, warnings, GEX overlay, and rendered message. Private routing fields and local absolute workspace paths are intentionally omitted.
