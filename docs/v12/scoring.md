# Premarket Report V1.2 Scoring

This file separates the scanner score from the tradeability grade. They are related but not identical.

## Scanner Score

The raw scanner score is:

```text
score = 50
score += min(25, abs(pm_change_pct) * 3)
if pm_vol >= 1000000:
    score += 10
if range_bias != "neutral":
    score += 10
score = min(100, round(score))
```

Range bias:

```text
range_position = (pm_price - pm_low) / max(pm_high - pm_low, 0.01) * 100

if range_position >= 70:
    range_bias = long
elif range_position <= 30:
    range_bias = short
else:
    range_bias = neutral
```

Score components:

- Base: 50 points
- Gap component: max 25 points
- Volume bonus: 10 points if `pm_vol >= 1,000,000`
- Range-position bonus: 10 points if price sits in top 30% or bottom 30% of the V1.2 range
- Hard cap: 100

## Why 91 vs 86 Can Happen

Example from 2026-06-04:

```text
SNDX:
base 50
abs gap 6.98 * 3 = 20.94
volume >= 1M = 10
range_bias short = 10
raw = 90.94 -> round = 91
```

```text
MRVL:
base 50
abs gap 5.23 * 3 = 15.69
volume >= 1M = 10
range_bias long = 10
raw = 85.69 -> round = 86
```

So the 5-point difference is almost entirely the gap component.

## Tradeability Adjusted Score

Category grade uses adjusted score, not raw scanner score:

```text
adjusted = scanner_score
if gapfade1 exists:
    adjusted += 8
if narrow_pm:
    adjusted -= 15
if rel_vol < 0.25:
    adjusted -= 5
adjusted = clamp(adjusted, 0, 100)
```

Thresholds:

```text
A  >= 82
B+ >= 74
B  >= 64
C  otherwise
```

Examples from 2026-06-04:

```text
SNDX raw 91 - narrow_pm 15 = 76 => B+
MRVL raw 86 - low_rel_vol 5 = 81 => B+
SMCI raw 92 - narrow_pm 15 - low_rel_vol 5 = 72 => B
```

This is why a higher scanner score can rank below a lower scanner score after warnings.

## GapFade1 Score

GapFade1 is a separate diagnostic score used in the GapFade1 section. It does not replace the scanner score.

Eligibility:

```text
pm_chg_pct >= 8.0
rel_vol < 1.5
```

Components:

- Gap/ADR 1.0x to 3.0x: +25
- Gap/ADR 0.5x to 1.0x: +10
- Gap/ADR > 5.0x: -10
- PM fading: +20
- PM pullback > 5% of gap: +15
- PM volume >= 1M: +20
- PM volume >= 500k: +10
- Thursday: +15
- Tuesday/Wednesday: +5
- RVOL < 0.7: +15
- RVOL < 1.0: +10
- Gap 8% to 15%: +10
- Gap > 20%: -5

Confidence:

```text
HIGH   score >= 70
MEDIUM score >= 45
LOW    otherwise
```

GapFade1 gets `+8` in the tradeability adjusted score because the setup has its own historical validation note in the report footer:

```text
60% Win | +3.0R avg | PF 2.0+ (83k events, 5yr)
```

## Category Ranking

The producer ranks candidates by:

```text
grade_rank desc, scanner_score desc, warning_count asc
```

Then:

- A-Setups: first 4 with `A` or `B+` and a trigger
- Watchlist: first 4 remaining with `B`
- Avoid: first 4 remaining

This means category assignment is deterministic from the structured payload.
