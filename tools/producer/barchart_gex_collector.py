#!/usr/bin/env python3
"""Collects Barchart gamma exposure snapshots and persists them to SQLite."""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from zoneinfo import ZoneInfo

import barchart_gex_parser

LOGGER = logging.getLogger("barchart_gex_collector")

TARGET_TICKERS = [
    "SPY",
    "QQQ",
    "IWM",
    "SMH",
    "AAPL",
    "MSFT",
    "NVDA",
    "META",
    "AMZN",
    "TSLA",
    "AMD",
    "MU",
    "PLTR",
    "SOXL",
    "COIN",
    "MSTR",
    "NFLX",
    "AVGO",
    "JPM",
    "GOOGL",
]

SESSION_PHASES = {"premarket", "open", "midday", "close"}
AT_FLIP_TOLERANCE_PCT = 0.25
DEFAULT_DB_PATH = Path("state/barchart_levels.sqlite")
SOURCE_NAME = "barchart_gamma_exposure"
EASTERN_TZ = ZoneInfo("America/New_York")
DECISION_WINDOW_PCT = 3.0
MAX_DECISION_LEVELS = 3
SESSION_PHASE_TIMES = {
    "premarket": (8, 30),
    "open": (9, 40),
    "midday": (12, 0),
    "close": (15, 45),
}


@dataclass
class Snapshot:
    ticker: str
    trading_day: str
    captured_at: str
    session_phase: str
    spot_price: float | None
    gamma_flip: float | None
    call_wall: float | None
    put_wall: float | None
    iv_rank: float | None
    max_call_oi_strike: float | None
    max_put_oi_strike: float | None
    dist_to_flip_pct: float | None
    dist_to_call_wall_pct: float | None
    dist_to_put_wall_pct: float | None
    regime: str | None
    near_support_levels_json: str | None = None
    near_resistance_levels_json: str | None = None
    extra: dict[str, object] = field(default_factory=dict)
    source: str = SOURCE_NAME

    def as_row(self) -> tuple:
        return (
            self.ticker,
            self.trading_day,
            self.captured_at,
            self.session_phase,
            self.spot_price,
            self.gamma_flip,
            self.call_wall,
            self.put_wall,
            self.iv_rank,
            self.max_call_oi_strike,
            self.max_put_oi_strike,
            self.dist_to_flip_pct,
            self.dist_to_call_wall_pct,
            self.dist_to_put_wall_pct,
            self.regime,
            self.near_support_levels_json,
            self.near_resistance_levels_json,
            self.source,
        )


class BarchartLevelsStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS barchart_levels_history (
                ticker TEXT NOT NULL,
                trading_day TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                session_phase TEXT NOT NULL,
                spot_price REAL,
                gamma_flip REAL,
                call_wall REAL,
                put_wall REAL,
                iv_rank REAL,
                max_call_oi_strike REAL,
                max_put_oi_strike REAL,
                dist_to_flip_pct REAL,
                dist_to_call_wall_pct REAL,
                dist_to_put_wall_pct REAL,
                regime TEXT,
                near_support_levels_json TEXT,
                near_resistance_levels_json TEXT,
                source TEXT NOT NULL,
                UNIQUE(ticker, trading_day, session_phase)
            )
            """
        )
        existing_columns = {
            row[1] for row in self.conn.execute("PRAGMA table_info(barchart_levels_history)").fetchall()
        }
        for column in ("near_support_levels_json", "near_resistance_levels_json"):
            if column not in existing_columns:
                self.conn.execute(
                    f"ALTER TABLE barchart_levels_history ADD COLUMN {column} TEXT"
                )
        self.conn.commit()

    def upsert(self, snapshot: Snapshot) -> None:
        columns = [
            "ticker",
            "trading_day",
            "captured_at",
            "session_phase",
            "spot_price",
            "gamma_flip",
            "call_wall",
            "put_wall",
            "iv_rank",
            "max_call_oi_strike",
            "max_put_oi_strike",
            "dist_to_flip_pct",
            "dist_to_call_wall_pct",
            "dist_to_put_wall_pct",
            "regime",
            "near_support_levels_json",
            "near_resistance_levels_json",
            "source",
        ]
        excluded = {"ticker", "trading_day", "session_phase"}
        assignments = ", ".join(f"{col}=excluded.{col}" for col in columns if col not in excluded)
        placeholders = ", ".join("?" for _ in columns)
        sql = f"""
            INSERT INTO barchart_levels_history ({", ".join(columns)})
            VALUES ({placeholders})
            ON CONFLICT(ticker, trading_day, session_phase) DO UPDATE SET
            {assignments}
        """
        self.conn.execute(sql, snapshot.as_row())
        self.conn.commit()

    def fetch_all(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        cur = self.conn.execute("SELECT * FROM barchart_levels_history")
        return cur.fetchall()

    def close(self) -> None:
        self.conn.close()


def dist_pct(spot: float | None, level: float | None) -> float | None:
    if spot is None or level is None:
        return None
    if spot == 0:
        return None
    return (spot - level) / spot * 100.0


def _max_strike(open_interest_by_strike: Mapping[float, float] | None) -> float | None:
    if not open_interest_by_strike:
        return None
    filtered = [(strike, oi) for strike, oi in open_interest_by_strike.items() if oi]
    if not filtered:
        return None
    return max(filtered, key=lambda item: item[1])[0]


def classify_regime(
    spot: float | None,
    gamma_flip: float | None,
    tolerance_pct: float = AT_FLIP_TOLERANCE_PCT,
) -> str | None:
    distance = dist_pct(spot, gamma_flip)
    if distance is None:
        return None
    if abs(distance) <= tolerance_pct:
        return "AT_FLIP"
    if spot is None or gamma_flip is None:
        return None
    if spot > gamma_flip:
        return "STABIL"
    return "VOLATIL"


def infer_session_phase(captured_at: datetime) -> str:
    eastern = captured_at.astimezone(EASTERN_TZ)
    current_minutes = eastern.hour * 60 + eastern.minute

    def distance(phase: str) -> int:
        hour, minute = SESSION_PHASE_TIMES[phase]
        return abs(current_minutes - (hour * 60 + minute))

    return min(SESSION_PHASES, key=distance)


def _extract_iv_rank(html: str) -> float | None:
    import re

    patterns = [
        r'"ivRank"\s*:\s*([0-9]{1,3}(?:\.[0-9]+)?)',
        r'IV Rank[^0-9]{0,25}([0-9]{1,3}(?:\.[0-9]+)?)%',
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, html, flags=re.IGNORECASE):
            context = html[max(0, match.start() - 25) : match.end() + 25]
            if "If IV Rank" in context:
                continue
            try:
                return float(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def build_snapshot_from_html(
    ticker: str,
    html: str,
    session_phase: str,
    captured_at: datetime,
    at_flip_tolerance_pct: float = AT_FLIP_TOLERANCE_PCT,
    trading_day_override: str | None = None,
) -> Snapshot:
    levels = barchart_gex_parser.extract_barchart_levels(html, include_debug=True)
    spot = levels.get("spot")
    gamma_flip = levels.get("gamma_flip")
    call_wall = levels.get("call_wall")
    put_wall = levels.get("put_wall")
    call_open_interest: Mapping[float, float] = levels.get("call_open_interest_by_strike", {}) or {}
    put_open_interest: Mapping[float, float] = levels.get("put_open_interest_by_strike", {}) or {}
    max_call = _max_strike(call_open_interest)
    max_put = _max_strike(put_open_interest)

    dist_flip = dist_pct(spot, gamma_flip)
    dist_call_wall = dist_pct(spot, call_wall)
    dist_put_wall = dist_pct(spot, put_wall)
    regime = classify_regime(spot, gamma_flip, tolerance_pct=at_flip_tolerance_pct)
    near_support_levels = levels.get("near_support_levels") or None
    near_resistance_levels = levels.get("near_resistance_levels") or None
    extra = {
        "all_flips": levels.get("all_flips") or [],
        "flip_interval": levels.get("flip_interval"),
        "rows_used": levels.get("rows_used"),
        "signed_gex_by_strike": levels.get("signed_gex_by_strike") or {},
        "net_gex_by_strike": levels.get("net_gex_by_strike") or {},
        "call_wall_exposure_by_strike": levels.get("call_wall_exposure_by_strike") or {},
        "put_wall_exposure_by_strike": levels.get("put_wall_exposure_by_strike") or {},
    }

    iv_rank = _extract_iv_rank(html)
    eastern_day = trading_day_override or captured_at.astimezone(EASTERN_TZ).date().isoformat()

    return Snapshot(
        ticker=ticker,
        trading_day=eastern_day,
        captured_at=captured_at.astimezone(UTC).isoformat(),
        session_phase=session_phase,
        spot_price=spot,
        gamma_flip=gamma_flip,
        call_wall=call_wall,
        put_wall=put_wall,
        iv_rank=iv_rank,
        max_call_oi_strike=max_call,
        max_put_oi_strike=max_put,
        dist_to_flip_pct=dist_flip,
        dist_to_call_wall_pct=dist_call_wall,
        dist_to_put_wall_pct=dist_put_wall,
        regime=regime,
        near_support_levels_json=json.dumps(near_support_levels) if near_support_levels else None,
        near_resistance_levels_json=json.dumps(near_resistance_levels) if near_resistance_levels else None,
        extra=extra,
    )


def collect_once(
    ticker: str,
    session_phase: str | None,
    at_flip_tolerance_pct: float,
    timeout: float,
    trading_day_override: str | None = None,
) -> Snapshot:
    captured_at = datetime.now(tz=UTC)
    html = barchart_gex_parser.fetch_barchart_html(ticker, timeout=timeout)
    resolved_phase = session_phase or infer_session_phase(captured_at)
    return build_snapshot_from_html(
        ticker=ticker,
        html=html,
        session_phase=resolved_phase,
        captured_at=captured_at,
        at_flip_tolerance_pct=at_flip_tolerance_pct,
        trading_day_override=trading_day_override,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect Barchart gamma snapshots")
    parser.add_argument(
        "--session-phase",
        choices=sorted(SESSION_PHASES),
        default=None,
        help="Session phase label (premarket, open, midday, close). If omitted, infer nearest ET phase.",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        default=None,
        help="Tickers to collect (default: mandated target list)",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help="SQLite database path (default: state/barchart_levels.sqlite)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout per ticker in seconds",
    )
    parser.add_argument(
        "--at-flip-tolerance-pct",
        type=float,
        default=AT_FLIP_TOLERANCE_PCT,
        help="Tolerance in percent for AT_FLIP regime classification",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and parse without persisting to SQLite",
    )
    parser.add_argument(
        "--trading-day-override",
        default=None,
        help="Persist this trading day (YYYY-MM-DD) instead of deriving it from current ET date",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    raw_tickers = args.tickers or TARGET_TICKERS
    tickers = [ticker.upper() for ticker in raw_tickers]
    store = BarchartLevelsStore(Path(args.db_path))
    success = 0
    failures: list[str] = []
    try:
        for ticker in tickers:
            try:
                snapshot = collect_once(
                    ticker=ticker,
                    session_phase=args.session_phase,
                    at_flip_tolerance_pct=args.at_flip_tolerance_pct,
                    timeout=args.timeout,
                    trading_day_override=args.trading_day_override,
                )
            except Exception as exc:  # noqa: BLE001 - want to keep snapshot loop running
                failures.append(ticker)
                LOGGER.exception("Failed to collect %s: %s", ticker, exc)
                continue

            if args.dry_run:
                LOGGER.info("Dry-run snapshot for %s: %s", ticker, snapshot)
            else:
                store.upsert(snapshot)
            success += 1
    finally:
        store.close()

    summary = (
        f"Captured {success}/{len(tickers)} snapshots "
        f"for phase={args.session_phase or "auto"} into {args.db_path}"
    )
    print(summary)
    if failures:
        print(f"Failures: {', '.join(failures)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
