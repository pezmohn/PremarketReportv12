# Premarket Report V1.2 Output Schema

The canonical JSON Schema is `schemas/premarket_report_v1_2.schema.json`. This document explains the intended producer payload.

## Report-Level Fields

Required:

- `ok`: scanner success boolean
- `error`: nullable error string
- `has_fetch_errors`: boolean
- `has_stale_flags`: boolean
- `candidate_count`: number of scanner candidates
- `run_started_at`: ISO timestamp
- `run_finished_at`: ISO timestamp
- `message`: rendered Telegram markdown
- `candidates`: raw enriched scanner candidates
- `claims`: execution claims derived from candidates
- `report_template`: `premarket_v1_2_a_watchlist_avoid`
- `direction_policy`: `gapfade`
- `trigger_policy`: `fib`
- `entry_policy`: `close_through`
- `gex_overlay`: object keyed by ticker, or null

## Candidate Fields

Core:

- `ticker`
- `prev_close`
- `prev_high`
- `prev_low`
- `pm_price`
- `pm_chg_pct`
- `pm_vol`
- `pm_high`
- `pm_low`
- `rel_vol`
- `market_cap_b`
- `pm_range_pct`
- `narrow_pm`
- `range_position`
- `range_bias`
- `score`
- `gap_up`
- `gapfade1`
- `levels`

`levels` contains:

- `PM_High`
- `PM_Low`
- `PDH`
- `PDL`
- `PDC`
- `Ext_-27.2`
- `Ext_127.2`
- `Fib_78.6`
- `Fib_50.0`
- `Fib_23.6`

## Claim Fields

Each claim is the trade-facing contract:

- `ticker`
- `pm_price`
- `scanner_score`
- `pm_change_pct`
- `expected_side`: `long` or `short`
- `tradeability_grade`: `A`, `B+`, `B`, or `C`
- `tradeability_read`: `Long Continuation`, `Short Continuation`, or `GapFade Short`
- `trigger_price`
- `trigger_name`
- `entry_policy`: `close_through`
- `setup_type`: `S1/S2` or `GapFade1`
- `warnings`

The rendered report should be rebuildable from `claims`, `candidates`, and `gex_overlay`.

## GEX Overlay Fields

Per ticker:

- `flip`
- `call_wall`
- `put_wall`
- `state_label`
- `bias`
- `index_regime_summary`
- `gamma_box`
- `trigger`
- `decision_summary`

`index_regime_summary` is repeated on every overlaid ticker because it is attached during GEX enrichment. The rendered report uses the first available overlay row for the header `Market Read` line.

## Full Historical JSON Examples

Use these as regression fixtures:

- `docs/v12/examples/report-2026-05-27.json`
- `docs/v12/examples/report-2026-05-28.json`
- `docs/v12/examples/report-2026-05-29.json`
- `docs/v12/examples/report-2026-06-01.json`
- `docs/v12/examples/report-2026-06-02.json`
- `docs/v12/examples/report-2026-06-03.json`
- `docs/v12/examples/report-2026-06-04.json`

They are real V1.2 snapshots with private routing and absolute workspace paths removed.
