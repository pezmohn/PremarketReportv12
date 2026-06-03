# Data Sources

## Polygon Snapshot

Use Polygon:

```text
GET /v2/snapshot/locale/us/markets/stocks/tickers
```

Required fields per ticker:

- `ticker`
- `min.c` as current snapshot price
- `min.av` as snapshot/premarket volume proxy
- `min.h` as range high in the original implementation
- `min.l` as range low in the original implementation
- `prevDay.c` as previous close
- `prevDay.h` as previous high
- `prevDay.l` as previous low
- `prevDay.v` as previous day volume

Authentication must come from `POLYGON_API_KEY` or equivalent runtime secret injection. Do not store the key in this repository.

## Polygon Reference

Use Polygon:

```text
GET /v3/reference/tickers/{ticker}
```

Required field:

- `results.market_cap`

Market cap is cacheable locally, but cache files must not be committed.

## Barchart Gamma Exposure

Use:

```text
https://www.barchart.com/stocks/quotes/{TICKER}/gamma-exposure
```

Parse the inline gamma payload and derive:

- current spot
- gamma flip
- call wall
- put wall
- IV rank when available
- call/put open interest by strike
- signed gamma exposure by strike
- nearby support levels
- nearby resistance levels

Gamma regime:

- `STABIL`: spot above gamma flip
- `VOLATIL`: spot below gamma flip
- `AT_FLIP`: absolute distance to flip <= 0.25%

## Local GEX History

Original implementation reads:

```text
state/barchart_levels.sqlite
```

It uses same-day `open` phase snapshots for:

- `SPY`
- `QQQ`
- `SMH`
- `IWM`

This produces the market read line, for example:

```text
Index flip bias mixed (SPY above flip, QQQ above flip, SMH below flip, IWM below flip).
```

Do not commit SQLite databases. Rebuilders can either provide their own local GEX history or omit the market read line.
