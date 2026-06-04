# V1.2 Producer Code

This directory contains the original workspace producer code that generated the V1.2 reports.

Main command:

```bash
python tools/producer/premarket-scanner.py --meta-json --with-gex
```

Cron-safe wrapper:

```bash
python tools/producer/premarket-scan-check.py --with-gex
```

Proposal flow:

```bash
python tools/producer/premarket-propose.py --with-gex
python tools/producer/premarket-verify.py --latest
python tools/producer/premarket-apply.py --latest
```

Required runtime dependencies:

- Python 3.11+
- `requests`
- Polygon API key
- local `state/barchart_levels.sqlite` for index GEX baselines

Original workspace paths:

- Polygon key: `sync/polygon-api-key.txt`
- market-cap cache: `scripts/data/mcap_cache.json`
- Barchart index store: `state/barchart_levels.sqlite`
- proposal archive: `state/proposals/premarket-scanner/`

The code is included for parity. A production rebuild should parameterize secrets and state paths instead of relying on the original OpenClaw workspace layout.

The public copy sanitizes the message target. Set this locally if you use the proposal/apply flow:

```bash
export PREMARKET_REPORT_TARGET="telegram:YOUR_CHAT_ID"
```
