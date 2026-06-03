# Algorithm

## 1. Build Candidate Universe

For every ticker in the Polygon snapshot:

1. Read current price from `min.c`.
2. Read snapshot volume from `min.av`.
3. Read previous close from `prevDay.c`.
4. Skip if price is below `$10`.
5. Skip if snapshot volume is below `500,000`.
6. Compute:

```text
pm_change_pct = (price - previous_close) / previous_close * 100
```

7. Skip if `abs(pm_change_pct) < 3`.
8. Fetch market cap from Polygon Reference.
9. Skip if market cap is missing or below `$2B`.

Keep at most 10 candidates after sorting by scanner score.

## 2. Compute Range Levels

Original implementation uses:

```text
range_high = min.h
range_low = min.l
range = max(range_high - range_low, 0.01)
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

Caveat: the label "premarket range" is not strictly proven by these fields. A production rebuild should prefer true 04:00-09:30 ET aggregate bars, but exact V1.2 parity uses the fields above.

## 3. Score Candidate

Start at 50:

```text
score = 50
score += min(25, abs(pm_change_pct) * 3)
if snapshot_volume >= 1_000_000: score += 10
if range_bias != neutral: score += 10
score = min(100, round(score))
```

Range bias:

```text
range_position = (price - range_low) / range * 100
if range_position >= 70: long
if range_position <= 30: short
else: neutral
```

## 4. Direction

Default direction:

- positive gap: `long`
- negative gap: `short`

GapFade1 override:

- if `pm_change_pct >= 8`
- and relative volume is below `1.5`
- direction becomes `short`
- label becomes `GapFade Short`

Relative volume proxy:

```text
relative_volume = snapshot_volume / (previous_day_volume / 6.5)
```

## 5. Trigger Selection

For long setups:

1. Check `Fib_78.6`, `Fib_50.0`, `Fib_23.6`.
2. Eligible if `level >= price * 0.995`.
3. Choose nearest eligible level.
4. Fallback to `PM_High`.

For short setups:

1. Check `Fib_23.6`, `Fib_50.0`, `Fib_78.6`.
2. Eligible if `level <= price * 1.005`.
3. Choose nearest eligible level.
4. Fallback to `PM_Low`.

Entry is valid only after the first 1-minute candle close through the trigger. Touch-only is explicitly invalid.

## 6. GEX Overlay

For the top candidates:

1. Fetch Barchart Gamma Exposure page.
2. Parse option rows.
3. Compute call wall and put wall from largest absolute exposure pockets.
4. Compute net GEX curve across strikes.
5. Gamma flip is the closest zero-crossing of the net GEX curve to spot.
6. Build local support/resistance clusters from signed GEX peaks around spot.
7. Mark gamma box when spot trades inside nearby support/resistance edges.

The report line includes:

```text
GEX: Flip {flip} | CallW {call_wall} | PutW {put_wall} | Box {box} | {state_label}
```

## 7. Ranking Buckets

Build claims for each candidate:

- ticker
- price
- scanner score
- expected side
- tradeability grade
- setup label
- trigger name and trigger price
- warnings

Warnings:

- `narrow_pm`: range percent below `0.5`
- `low_rel_vol`: relative volume below `0.25`
- `gamma_box`: GEX overlay says price is inside a gamma box

Grades:

```text
adjusted_score = scanner_score
if GapFade1: adjusted_score += 8
if narrow range: adjusted_score -= 15
if relative_volume < 0.25: adjusted_score -= 5

A  = adjusted_score >= 82
B+ = adjusted_score >= 74
B  = adjusted_score >= 64
C  = otherwise
```

Buckets:

- A-Setups: `A` or `B+`, trigger required, max 4
- Watchlist: `B`, max 4
- Avoid: remaining candidates, max 4

## 8. Verification Gates

Before emitting the message:

- run only during `09:00-09:30 ET`
- proposal age must be <= 300 seconds
- scanner must exit with code 0
- no fetch errors
- no stale/unreliable flags
- non-empty message

If any gate fails, emit no report.
