# Rebuild Checklist

Use this checklist to recreate the report behavior from scratch.

## Runtime Flow

Implement these stages:

1. `scan_premarket`
   - fetch Polygon all-stock snapshot
   - filter by price, change, volume, market cap
   - compute levels, range position, score, GapFade1
   - return top 10 sorted by score

2. `attach_gex_overlay`
   - fetch Barchart GEX for top candidates
   - compute flip, walls, gamma box, state label
   - merge same-day index flip summary from SPY/QQQ/SMH/IWM if available

3. `build_claims`
   - derive direction
   - choose Fib trigger
   - assign grade
   - attach warnings

4. `rank_report_candidates`
   - A-setups first
   - Watchlist second
   - Avoid third

5. `format_telegram`
   - render Telegram Markdown exactly in the V1.2 shape

6. `verify`
   - reject stale, wrong-phase, failed, empty, or unreliable reports

## Function Equivalents

The original workspace function names map to these responsibilities:

| Function | Responsibility |
| --- | --- |
| `get_snapshot` | Polygon all-stock snapshot fetch |
| `fetch_market_caps` | Polygon reference market cap fetch/cache |
| `calc_levels` | PM/PD/Fib/extension levels |
| `calc_range_position` | long/short/neutral range bias |
| `score_setup` | 0-100 candidate score |
| `calc_gapfade1` | low-RVOL gap-up fade override |
| `direction_for_candidate` | final long/short expected side |
| `choose_fib_trigger` | trigger name and price |
| `tradeability_grade` | A/B+/B/C grade |
| `build_claims` | structured trade claims |
| `rank_report_candidates` | A/watchlist/avoid buckets |
| `format_telegram` | Telegram report renderer |

## Output Sections

Render sections in this order:

1. Header
2. Engine line
3. Candidate count and entry policy
4. Optional market read
5. A-Setups / Tradeable nur nach Trigger
6. Watchlist / Nur bei sauberem Trigger
7. Avoid / Kein First-Choice-Trade
8. Alle Scanner-Kandidaten / Audit Trail
9. Optional GapFade1 section
10. Entry, exit, and daily risk rules

## Secret Hygiene

Before committing:

```bash
git status -sb
git ls-files
```

Only `.env.example` may contain the placeholder:

```text
POLYGON_API_KEY=replace_me
```
