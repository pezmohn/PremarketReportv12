# Premarket Report V1.2 Definition

Secret-free source definition for rebuilding the Telegram **Premarket Report V1.2**:

```text
Engine: Gapfade Direction + Fib Trigger + Close-through Entry
```

This repository documents the report contract, data sources, filters, scoring, trigger rules, GEX overlay, verification gates, output format, historical producer snapshots, and the original producer code. It intentionally does **not** contain API keys, SQLite state, logs, or private routing data.

## What The Report Does

The report builds a US equities premarket watchlist:

1. Pull a live Polygon stock snapshot.
2. Filter for liquid large-cap movers.
3. Score each candidate by gap size, volume, and range position.
4. Set direction:
   - gap up defaults to long continuation
   - gap down defaults to short continuation
   - GapFade1 overrides selected low-RVOL gap-ups to short fade
5. Select a Fib trigger.
6. Require close-through entry: first 1-minute candle close through the trigger.
7. Add Barchart Gamma Exposure context for top candidates.
8. Verify report freshness and fail closed if the data is stale or invalid.

## Rebuild Contract

Use these files as the source of truth:

- [config/report.v1.2.yaml](config/report.v1.2.yaml) - machine-readable report definition
- [docs/v12/producer_spec.md](docs/v12/producer_spec.md) - full formal V1.2 producer spec
- [docs/v12/scoring.md](docs/v12/scoring.md) - scanner score, tradeability grade, category math
- [docs/v12/output_schema.md](docs/v12/output_schema.md) - payload contract and fixture list
- [docs/algorithm.md](docs/algorithm.md) - step-by-step logic
- [docs/data-sources.md](docs/data-sources.md) - required external and local data contracts
- [schemas/premarket_report_v1_2.schema.json](schemas/premarket_report_v1_2.schema.json) - structured output schema
- [examples/report-2026-06-02.md](examples/report-2026-06-02.md) - example Telegram output
- [docs/v12/examples/](docs/v12/examples) - 7 real sanitized V1.2 JSON/Markdown snapshots
- [tools/producer/](tools/producer) - original producer, proposal, verification, EOD validation, and Barchart GEX helper code

## Included Historical Fixtures

Real V1.2 producer snapshots are included for:

```text
2026-05-27
2026-05-28
2026-05-29
2026-06-01
2026-06-02
2026-06-03
2026-06-04
```

Each fixture has a structured JSON payload and the rendered Telegram markdown. The JSON keeps real candidates, scores, warnings, claims, ranking behavior, and GEX overlay. Private Telegram routing and absolute local command paths are omitted.

## Original Producer Code

Run the parity producer from this repo with:

```bash
python tools/producer/premarket-scanner.py --meta-json --with-gex
```

Cron-safe wrapper:

```bash
python tools/producer/premarket-scan-check.py --with-gex
```

The copied producer still assumes the original workspace layout for state and caches. Treat it as parity code first, production scaffold second.

## Required Secrets

Runtime implementations need a Polygon API key, supplied via environment:

```bash
cp .env.example .env
# edit POLYGON_API_KEY locally
```

Do not commit `.env`, `sync/`, `state/`, SQLite files, or logs. `.gitignore` blocks those paths.

## Known Quality Caveat

The original workspace implementation labeled `min.h/min.l` as premarket high/low. Polygon snapshot `min` fields may represent the current aggregate minute rather than the full premarket session range. For exact rebuild parity, keep the definition as written. For production-grade trading use, replace this with a true 04:00-09:30 ET premarket range from aggregate bars.
