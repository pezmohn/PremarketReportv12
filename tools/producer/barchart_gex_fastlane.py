#!/usr/bin/env python3
"""Thin ad-hoc Barchart GEX path with separate storage and compact output."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from barchart_gex_collector import (
    AT_FLIP_TOLERANCE_PCT,
    DEFAULT_DB_PATH,
    Snapshot,
    collect_once,
)

DEFAULT_FASTLANE_DB_PATH = Path("state/barchart_fastlane.sqlite")
FASTLANE_SOURCE_NAME = "barchart_gamma_exposure_fastlane"
FASTLANE_SESSION_PHASE = "fastlane"
INDEX_FLIP_TICKERS = ("SPY", "QQQ", "SMH", "IWM")
ETF_STYLE_TICKERS = {"SPY", "QQQ", "IWM", "SMH", "SOXL"}


@dataclass
class Actionability:
    bias: str
    conviction: int
    score_label: str
    state_label: str
    nearest_risk: str
    index_regime_bias: str
    index_regime_summary: str
    summary: str


@dataclass
class DecisionPlan:
    setup: str
    trigger: str
    invalidation: str
    path_if_triggered: str
    summary: str


@dataclass
class RankingEntry:
    ticker: str
    attention_score: float
    pin_risk_score: float
    breakout_score: float
    mean_reversion_score: float
    structure_clarity_score: float
    summary: str


@dataclass
class SnapshotComparison:
    baseline_phase: str | None
    baseline_captured_at: str | None
    regime_changed: bool
    flip_change_pct: float | None
    call_wall_change_pct: float | None
    put_wall_change_pct: float | None
    summary: str


@dataclass
class StructureAnalysis:
    support_levels: list[float]
    resistance_levels: list[float]
    support_cluster_levels: list[float]
    resistance_cluster_levels: list[float]
    support_cluster: str | None
    resistance_cluster: str | None
    support_cluster_width: float | None
    resistance_cluster_width: float | None
    support_density: int
    resistance_density: int
    support_peak_count: int
    resistance_peak_count: int
    inside_gamma_box: bool
    gamma_box: str | None
    open_air_side: str | None
    structure_label: str
    summary: str


@dataclass
class FastLaneBatchResult:
    batch_id: str
    succeeded: list[Snapshot]
    failed: dict[str, str]
    db_path: Path


class BarchartLevelsHistoryStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row

    def fetch_latest_baseline(
        self,
        ticker: str,
        *,
        trading_day: str,
        preferred_phase: str | None = None,
    ) -> sqlite3.Row | None:
        if preferred_phase:
            row = self.conn.execute(
                """
                SELECT *
                FROM barchart_levels_history
                WHERE ticker = ? AND trading_day = ? AND session_phase = ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (ticker, trading_day, preferred_phase),
            ).fetchone()
            if row is not None:
                return row

        return self.conn.execute(
            """
            SELECT *
            FROM barchart_levels_history
            WHERE ticker = ? AND trading_day = ?
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (ticker, trading_day),
        ).fetchone()

    def close(self) -> None:
        self.conn.close()


class BarchartFastLaneStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS barchart_fastlane_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL,
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
                source TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_barchart_fastlane_runs_batch_ticker
            ON barchart_fastlane_runs (batch_id, ticker)
            """
        )
        self.conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_barchart_fastlane_runs_ticker_captured
            ON barchart_fastlane_runs (ticker, captured_at DESC)
            """
        )
        self.conn.commit()

    def insert(self, snapshot: Snapshot, batch_id: str) -> None:
        self.conn.execute(
            """
            INSERT INTO barchart_fastlane_runs (
                batch_id,
                ticker,
                trading_day,
                captured_at,
                session_phase,
                spot_price,
                gamma_flip,
                call_wall,
                put_wall,
                iv_rank,
                max_call_oi_strike,
                max_put_oi_strike,
                dist_to_flip_pct,
                dist_to_call_wall_pct,
                dist_to_put_wall_pct,
                regime,
                near_support_levels_json,
                near_resistance_levels_json,
                source
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                snapshot.ticker,
                snapshot.trading_day,
                snapshot.captured_at,
                snapshot.session_phase,
                snapshot.spot_price,
                snapshot.gamma_flip,
                snapshot.call_wall,
                snapshot.put_wall,
                snapshot.iv_rank,
                snapshot.max_call_oi_strike,
                snapshot.max_put_oi_strike,
                snapshot.dist_to_flip_pct,
                snapshot.dist_to_call_wall_pct,
                snapshot.dist_to_put_wall_pct,
                snapshot.regime,
                snapshot.near_support_levels_json,
                snapshot.near_resistance_levels_json,
                snapshot.source,
            ),
        )
        self.conn.commit()

    def fetch_all(self) -> list[sqlite3.Row]:
        self.conn.row_factory = sqlite3.Row
        return self.conn.execute(
            """
            SELECT *
            FROM barchart_fastlane_runs
            ORDER BY id
            """
        ).fetchall()

    def close(self) -> None:
        self.conn.close()


def _as_fastlane_snapshot(snapshot: Snapshot) -> Snapshot:
    snapshot.source = FASTLANE_SOURCE_NAME
    snapshot.session_phase = FASTLANE_SESSION_PHASE
    return snapshot


def _format_level(level: float | None) -> str:
    return f"{level:.2f}" if level is not None else "-"


def _format_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def _pct_change(current: float | None, baseline: float | None) -> float | None:
    if current is None or baseline in (None, 0):
        return None
    return (current - baseline) / baseline * 100.0


def _nearest_wall(snapshot: Snapshot) -> tuple[str | None, float | None, float | None]:
    candidates = [
        ("CW", snapshot.call_wall, snapshot.dist_to_call_wall_pct),
        ("PW", snapshot.put_wall, snapshot.dist_to_put_wall_pct),
    ]
    valid = [item for item in candidates if item[1] is not None and item[2] is not None]
    if not valid:
        return (None, None, None)
    return min(valid, key=lambda item: abs(float(item[2])))


def build_index_regime_bias(snapshot: Snapshot, history_store: BarchartLevelsHistoryStore | None) -> tuple[str, str]:
    if history_store is None:
        return ("unknown", "No index-flip context available.")

    aligned_above = 0
    aligned_below = 0
    seen = 0
    details: list[str] = []
    for ticker in INDEX_FLIP_TICKERS:
        baseline = history_store.fetch_latest_baseline(
            ticker,
            trading_day=snapshot.trading_day,
            preferred_phase="open",
        )
        if baseline is None:
            continue
        flip = baseline["gamma_flip"]
        spot = baseline["spot_price"]
        if flip is None or spot is None:
            continue
        seen += 1
        above = spot > flip
        if above:
            aligned_above += 1
        else:
            aligned_below += 1
        details.append(f"{ticker} {'above' if above else 'below'} flip")

    if seen == 0:
        return ("unknown", "No index-flip baselines available for today.")
    if aligned_above >= 3:
        return ("bullish", f"Index flip bias bullish ({', '.join(details)}).")
    if aligned_below >= 3:
        return ("bearish", f"Index flip bias bearish ({', '.join(details)}).")
    return ("mixed", f"Index flip bias mixed ({', '.join(details)}).")


def _coerce_float_levels(raw: object) -> list[float]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    if not isinstance(raw, list):
        return []
    values: list[float] = []
    for item in raw:
        try:
            values.append(float(item))
        except (TypeError, ValueError):
            continue
    return sorted(values)


def _cluster_summary(levels: list[float]) -> tuple[str | None, float | None, int]:
    if not levels:
        return (None, None, 0)
    if len(levels) == 1:
        return (f"{levels[0]:.2f}", 0.0, 1)
    return (f"{levels[0]:.2f}-{levels[-1]:.2f}", levels[-1] - levels[0], len(levels))


def _coerce_signed_map(raw: object) -> dict[float, float]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return {}
    if not isinstance(raw, dict):
        return {}
    result: dict[float, float] = {}
    for key, value in raw.items():
        try:
            result[float(key)] = float(value)
        except (TypeError, ValueError):
            continue
    return dict(sorted(result.items()))


def _extract_density_cluster(
    signed_map: dict[float, float],
    *,
    spot: float | None,
    side: str,
    decision_levels: list[float],
    decision_window_pct: float = 6.0,
) -> tuple[list[float], int]:
    if spot is None or not signed_map:
        return (decision_levels, len(decision_levels))

    lower = spot * (1.0 - decision_window_pct / 100.0)
    upper = spot * (1.0 + decision_window_pct / 100.0)
    candidates: list[tuple[float, float]] = []
    for strike, signed_value in signed_map.items():
        if not (lower <= strike <= upper):
            continue
        if side == "below" and strike >= spot:
            continue
        if side == "above" and strike <= spot:
            continue
        strength = abs(signed_value)
        if strength <= 0:
            continue
        candidates.append((strike, strength))

    if not candidates:
        return (decision_levels, len(decision_levels))

    candidates.sort(key=lambda item: (-item[1], abs(item[0] - spot), item[0]))
    seed_levels = [strike for strike, _ in candidates[:3]]
    for level in decision_levels:
        if level not in seed_levels:
            seed_levels.append(level)

    seed_levels = sorted(seed_levels)
    if not seed_levels:
        return (decision_levels, len(decision_levels))

    clustered: list[float] = []
    merge_threshold = max(0.5, spot * 0.015)
    for strike, _strength in sorted(candidates, key=lambda item: item[0]):
        if any(abs(strike - seed) <= merge_threshold for seed in seed_levels):
            clustered.append(strike)

    merged = sorted({round(level, 2) for level in (clustered or seed_levels)})
    return (merged, len(candidates))


def build_structure_analysis(snapshot: Snapshot) -> StructureAnalysis:
    support_levels = _coerce_float_levels(snapshot.near_support_levels_json)
    resistance_levels = _coerce_float_levels(snapshot.near_resistance_levels_json)
    signed_map = _coerce_signed_map(snapshot.extra.get("signed_gex_by_strike", {}))
    support_cluster_levels, support_peak_count = _extract_density_cluster(
        signed_map,
        spot=snapshot.spot_price,
        side="below",
        decision_levels=support_levels,
    )
    resistance_cluster_levels, resistance_peak_count = _extract_density_cluster(
        signed_map,
        spot=snapshot.spot_price,
        side="above",
        decision_levels=resistance_levels,
    )
    support_cluster, support_width, support_density = _cluster_summary(support_cluster_levels)
    resistance_cluster, resistance_width, resistance_density = _cluster_summary(resistance_cluster_levels)

    support_floor = support_cluster_levels[-1] if support_cluster_levels else (support_levels[-1] if support_levels else snapshot.put_wall)
    resistance_ceiling = resistance_cluster_levels[0] if resistance_cluster_levels else (resistance_levels[0] if resistance_levels else snapshot.call_wall)

    inside_gamma_box = False
    gamma_box = None
    if snapshot.spot_price is not None and support_floor is not None and resistance_ceiling is not None:
        low = min(support_floor, resistance_ceiling)
        high = max(support_floor, resistance_ceiling)
        inside_gamma_box = low <= snapshot.spot_price <= high
        if inside_gamma_box:
            gamma_box = f"{low:.2f}-{high:.2f}"

    open_air_side = None
    if resistance_density == 0:
        open_air_side = "above"
    elif support_density == 0:
        open_air_side = "below"

    if inside_gamma_box:
        structure_label = "price_inside_gamma_box"
        summary = f"Spot is trading inside the gamma box {gamma_box}."
    elif resistance_density >= 2 and support_density >= 1:
        structure_label = "clustered_resistance_above"
        summary = f"Dense resistance cluster above ({resistance_cluster}) with nearby support {support_cluster or '-'}."
    elif resistance_density >= 2 and support_density == 0:
        structure_label = "resistance_congestion_only"
        summary = f"Heavy resistance congestion above ({resistance_cluster}) with little nearby support detail."
    elif support_density >= 2 and resistance_density == 0:
        structure_label = "support_congestion_only"
        summary = f"Heavy support congestion below ({support_cluster}) with little nearby resistance detail."
    elif resistance_density >= 1 and support_density >= 1:
        structure_label = "balanced_gamma_map"
        summary = f"Support {support_cluster or '-'} and resistance {resistance_cluster or '-'} frame the move."
    elif open_air_side == "above":
        structure_label = "open_air_above"
        summary = "Little nearby resistance structure above current price."
    elif open_air_side == "below":
        structure_label = "open_air_below"
        summary = "Little nearby support structure below current price."
    else:
        structure_label = "single_level_structure"
        summary = f"Nearest support {support_cluster or '-'} and resistance {resistance_cluster or '-'} define the map."

    return StructureAnalysis(
        support_levels=support_levels,
        resistance_levels=resistance_levels,
        support_cluster_levels=support_cluster_levels,
        resistance_cluster_levels=resistance_cluster_levels,
        support_cluster=support_cluster,
        resistance_cluster=resistance_cluster,
        support_cluster_width=support_width,
        resistance_cluster_width=resistance_width,
        support_density=support_density,
        resistance_density=resistance_density,
        support_peak_count=support_peak_count,
        resistance_peak_count=resistance_peak_count,
        inside_gamma_box=inside_gamma_box,
        gamma_box=gamma_box,
        open_air_side=open_air_side,
        structure_label=structure_label,
        summary=summary,
    )


def build_state_label(snapshot: Snapshot) -> str:
    flip_dist = snapshot.dist_to_flip_pct
    nearest_label, _, nearest_dist = _nearest_wall(snapshot)

    if flip_dist is None:
        return "unclassified"
    if abs(flip_dist) <= AT_FLIP_TOLERANCE_PCT:
        return "at_flip"
    if abs(flip_dist) <= 1.0:
        return "near_flip"

    if nearest_label == "CW" and nearest_dist is not None and -1.0 <= nearest_dist <= 0.35:
        return "pressed_into_call_wall"
    if nearest_label == "PW" and nearest_dist is not None and -0.35 <= nearest_dist <= 1.0:
        return "sitting_on_put_wall"
    if flip_dist > 0:
        return "comfortably_above_flip"
    return "comfortably_below_flip"


def build_actionability(
    snapshot: Snapshot,
    structure: StructureAnalysis,
    index_regime_bias: str = "unknown",
    index_regime_summary: str = "No index-flip context available.",
) -> Actionability:
    mr_score = 0.0
    exp_score = 0.0
    nearest_label, nearest_level, nearest_dist = _nearest_wall(snapshot)
    flip_dist = snapshot.dist_to_flip_pct
    is_etf_style = snapshot.ticker in ETF_STYLE_TICKERS

    if snapshot.regime == "STABIL":
        mr_score += 1.5
    elif snapshot.regime == "VOLATIL":
        exp_score += 1.5

    if index_regime_bias == "bullish":
        mr_score += 2.0
    elif index_regime_bias == "bearish":
        exp_score += 2.0
    elif index_regime_bias == "mixed":
        mr_score += 0.25
        exp_score += 0.25

    if flip_dist is not None:
        if flip_dist > 0.5:
            mr_score += 2.5 if is_etf_style else 1.5
        elif flip_dist < -0.5:
            exp_score += 2.5 if is_etf_style else 1.5
        if abs(flip_dist) <= AT_FLIP_TOLERANCE_PCT:
            mr_score -= 1.5
            exp_score -= 1.5
        elif abs(flip_dist) <= 1.0:
            mr_score += 0.5 if flip_dist > 0 else 0
            exp_score += 0.5 if flip_dist < 0 else 0

    if nearest_label == "CW" and nearest_dist is not None:
        if -1.0 <= nearest_dist <= 0.35:
            mr_score += 0.25 if is_etf_style else 0.75
            exp_score += 0.5 if is_etf_style else 0.25
        elif nearest_dist < -2.5 and snapshot.regime == "VOLATIL":
            exp_score += 0.75
    if nearest_label == "PW" and nearest_dist is not None:
        if -0.35 <= nearest_dist <= 1.0:
            mr_score += 0.15 if snapshot.regime == "STABIL" else 0
            exp_score += 0.35 if snapshot.regime == "VOLATIL" else 0.15

    if structure.inside_gamma_box:
        mr_score += 1.5
    if structure.resistance_density >= 2:
        mr_score += 0.5
    if structure.resistance_density >= 4:
        mr_score += 0.25
    if structure.support_density >= 4 and snapshot.regime == "VOLATIL":
        exp_score += 0.75
    if structure.open_air_side == "above" and snapshot.regime == "VOLATIL":
        exp_score += 1.0

    delta = mr_score - exp_score
    if delta >= 2:
        bias = "mean_reversion"
        conviction = min(3, int(abs(delta)) if abs(delta) >= 1 else 1)
    elif delta <= -2:
        bias = "expansion"
        conviction = min(3, int(abs(delta)) if abs(delta) >= 1 else 1)
    else:
        bias = "neutral"
        conviction = 1 if abs(delta) >= 0.75 else 0

    score_label = (
        f"MR+{delta:.2f}" if delta > 0 else f"EXP+{abs(delta):.2f}" if delta < 0 else "NEUTRAL"
    )
    state_label = build_state_label(snapshot)

    nearest_risk = "no_wall_context"
    if nearest_label == "CW" and nearest_level is not None:
        nearest_risk = "call_wall_above" if (nearest_dist or 0) <= 0 else "call_wall_below"
    elif nearest_label == "PW" and nearest_level is not None:
        nearest_risk = "put_wall_below" if (nearest_dist or 0) >= 0 else "put_wall_above"
    elif state_label == "at_flip":
        nearest_risk = "at_flip"

    if bias == "mean_reversion":
        summary = "Dealer damping favors fades before trend continuation."
    elif bias == "expansion":
        summary = "Below-flip positioning favors continuation once price leaves the flip cleanly."
    else:
        summary = "No clean edge, wait for a clearer move away from the flip."

    if index_regime_bias == "bullish":
        summary = f"Index flips lean supportive. {summary}"
    elif index_regime_bias == "bearish":
        summary = f"Index flips lean heavy. {summary}"
    elif index_regime_bias == "mixed":
        summary = f"Index flips are mixed. {summary}"

    if structure.inside_gamma_box and bias == "mean_reversion":
        summary = f"Gamma box {structure.gamma_box} points to chop and two-way trade inside the zone."
    elif structure.resistance_density >= 4 and bias == "mean_reversion":
        summary = f"Dense resistance pocket around {structure.resistance_cluster} is a decision zone, not clean upside air."
    elif structure.resistance_density >= 2 and bias == "mean_reversion":
        summary = f"Clustered resistance around {structure.resistance_cluster} looks like a likely test area, not a guaranteed rejection."
    elif structure.open_air_side == "above" and bias == "expansion":
        summary = "Open air above leaves room for continuation if price stays below flip pressure."

    return Actionability(
        bias=bias,
        conviction=conviction,
        score_label=score_label,
        state_label=state_label,
        nearest_risk=nearest_risk,
        index_regime_bias=index_regime_bias,
        index_regime_summary=index_regime_summary,
        summary=summary,
    )


def build_decision_plan(
    snapshot: Snapshot,
    actionability: Actionability,
    structure: StructureAnalysis,
) -> DecisionPlan:
    spot = snapshot.spot_price
    flip = snapshot.gamma_flip
    nearest_label, nearest_level, nearest_dist = _nearest_wall(snapshot)
    resistance_edge = structure.resistance_cluster_levels[-1] if structure.resistance_cluster_levels else nearest_level
    support_edge = structure.support_cluster_levels[0] if structure.support_cluster_levels else None

    if structure.inside_gamma_box and resistance_edge is not None:
        setup = "pin_risk_high"
        trigger = f"Need clean acceptance above {resistance_edge:.2f} to escape the gamma box."
        invalidation = f"Failure to hold above {resistance_edge:.2f} keeps chop risk elevated."
        path_if_triggered = f"If {resistance_edge:.2f} clears, upside should have cleaner air beyond the local pocket."
        summary = f"High pin risk inside {structure.gamma_box}; wait for clearance above {resistance_edge:.2f}."
    elif actionability.bias == "mean_reversion" and structure.resistance_density >= 2 and resistance_edge is not None:
        setup = "decision_zone_first"
        trigger = f"Only treat upside as cleaner if price reclaims {resistance_edge:.2f}."
        invalidation = f"Failure to hold progress through {resistance_edge:.2f} keeps the decision zone unresolved."
        path_if_triggered = f"Above {resistance_edge:.2f}, the resistance pocket is cleared and momentum has cleaner room."
        summary = f"Resistance pocket around {structure.resistance_cluster} is a break-or-reject decision zone first."
    elif actionability.bias == "expansion" and structure.open_air_side == "above" and flip is not None:
        setup = "breakout_continuation"
        trigger = f"Stay below flip pressure and keep momentum away from {flip:.2f}."
        invalidation = f"A move back toward {flip:.2f} would kill the clean-air continuation case."
        path_if_triggered = "Open air above favors trend continuation rather than immediate pinning."
        summary = "Continuation setup, open air above leaves room if momentum persists."
    elif support_edge is not None and flip is not None:
        setup = "range_frame"
        trigger = f"Above flip {flip:.2f} the path stays stabilizing, below support {support_edge:.2f} the map changes."
        invalidation = f"Lose {support_edge:.2f} and the current framing breaks."
        path_if_triggered = f"As long as {support_edge:.2f} holds, expect rotation inside the current structure."
        resistance_text = f"{resistance_edge:.2f}" if resistance_edge is not None else "n/a"
        summary = f"Range-framed setup between support {support_edge:.2f} and resistance {resistance_text}."
    else:
        setup = "wait_for_clarity"
        trigger = "Wait for cleaner separation from the flip and local walls."
        invalidation = "Current structure is too noisy for a clean trigger."
        path_if_triggered = "Once price separates from the current pocket, direction should clarify."
        summary = "No clean trigger yet, structure still noisy."

    return DecisionPlan(
        setup=setup,
        trigger=trigger,
        invalidation=invalidation,
        path_if_triggered=path_if_triggered,
        summary=summary,
    )


def build_ranking_entry(
    snapshot: Snapshot,
    actionability: Actionability,
    structure: StructureAnalysis,
    decision: DecisionPlan,
) -> RankingEntry:
    pin_risk_score = 0.0
    breakout_score = 0.0
    mean_reversion_score = 0.0
    structure_clarity_score = 0.0

    if structure.inside_gamma_box:
        pin_risk_score += 4.0
    pin_risk_score += min(3.0, float(structure.resistance_density))
    if decision.setup == "pin_risk_high":
        pin_risk_score += 3.0

    if actionability.bias == "expansion":
        breakout_score += 4.0
    if structure.open_air_side == "above":
        breakout_score += 2.0
    if decision.setup == "breakout_continuation":
        breakout_score += 3.0
    if structure.resistance_density <= 1:
        breakout_score += 1.0

    if actionability.bias == "mean_reversion":
        mean_reversion_score += 4.0
    mean_reversion_score += min(3.0, float(structure.resistance_density))
    if structure.inside_gamma_box:
        mean_reversion_score += 2.0

    structure_clarity_score += 2.0 if structure.support_density > 0 else 0.0
    structure_clarity_score += 2.0 if structure.resistance_density > 0 else 0.0
    if structure.gamma_box:
        structure_clarity_score += 2.0
    if structure.support_cluster_width is not None and structure.support_cluster_width <= 1.5:
        structure_clarity_score += 1.0
    if structure.resistance_cluster_width is not None and structure.resistance_cluster_width <= 1.5:
        structure_clarity_score += 1.0

    attention_score = max(pin_risk_score, breakout_score, mean_reversion_score) + structure_clarity_score * 0.35

    if pin_risk_score >= breakout_score and pin_risk_score >= mean_reversion_score:
        summary = f"{snapshot.ticker}: highest pin risk, needs clear trigger level before trusting continuation."
    elif breakout_score >= mean_reversion_score:
        summary = f"{snapshot.ticker}: best breakout candidate if the trigger clears."
    else:
        summary = f"{snapshot.ticker}: cleanest decision-zone / mean-reversion map in the current watchlist."

    return RankingEntry(
        ticker=snapshot.ticker,
        attention_score=round(attention_score, 2),
        pin_risk_score=round(pin_risk_score, 2),
        breakout_score=round(breakout_score, 2),
        mean_reversion_score=round(mean_reversion_score, 2),
        structure_clarity_score=round(structure_clarity_score, 2),
        summary=summary,
    )


def build_comparison(snapshot: Snapshot, baseline_row: sqlite3.Row | None) -> SnapshotComparison:
    if baseline_row is None:
        return SnapshotComparison(
            baseline_phase=None,
            baseline_captured_at=None,
            regime_changed=False,
            flip_change_pct=None,
            call_wall_change_pct=None,
            put_wall_change_pct=None,
            summary="No main-session baseline yet.",
        )

    baseline_phase = baseline_row["session_phase"]
    baseline_regime = baseline_row["regime"]
    flip_change_pct = _pct_change(snapshot.gamma_flip, baseline_row["gamma_flip"])
    call_wall_change_pct = _pct_change(snapshot.call_wall, baseline_row["call_wall"])
    put_wall_change_pct = _pct_change(snapshot.put_wall, baseline_row["put_wall"])
    regime_changed = baseline_regime != snapshot.regime

    change_parts: list[str] = []
    if regime_changed:
        change_parts.append(f"regime {baseline_regime or '-'} -> {snapshot.regime or '-'}")
    else:
        change_parts.append(f"regime unchanged vs {baseline_phase}")

    for label, value in (("flip", flip_change_pct), ("cw", call_wall_change_pct), ("pw", put_wall_change_pct)):
        if value is not None and abs(value) >= 0.25:
            change_parts.append(f"{label} {_format_pct(value)}")

    summary = ", ".join(change_parts) if change_parts else f"No meaningful shift vs {baseline_phase}."
    return SnapshotComparison(
        baseline_phase=baseline_phase,
        baseline_captured_at=baseline_row["captured_at"],
        regime_changed=regime_changed,
        flip_change_pct=flip_change_pct,
        call_wall_change_pct=call_wall_change_pct,
        put_wall_change_pct=put_wall_change_pct,
        summary=summary,
    )


def build_trade_implications(
    snapshot: Snapshot,
    actionability: Actionability,
    structure: StructureAnalysis,
    decision: DecisionPlan,
) -> list[str]:
    implications: list[str] = [
        f"Bias: {actionability.bias} ({actionability.score_label}, conviction {actionability.conviction}/3).",
        actionability.summary,
    ]
    nearest_label, nearest_level, nearest_dist = _nearest_wall(snapshot)
    if nearest_label and nearest_level is not None and nearest_dist is not None:
        implications.append(
            f"Nearest {nearest_label} {_format_level(nearest_level)} sits {_format_pct(nearest_dist)} from spot."
        )
    implications.append(f"Structure: {structure.summary}")
    implications.append(f"Decision: {decision.summary}")
    return implications[:5]


def format_snapshot_compact(
    snapshot: Snapshot,
    actionability: Actionability,
    comparison: SnapshotComparison,
    structure: StructureAnalysis,
    decision: DecisionPlan,
) -> str:
    nearest_label, nearest_level, nearest_dist = _nearest_wall(snapshot)
    nearest_text = "-"
    if nearest_label and nearest_level is not None and nearest_dist is not None:
        nearest_text = f"{nearest_label} {_format_level(nearest_level)} ({_format_pct(nearest_dist)})"

    parts = [
        f"{snapshot.ticker} {_format_level(snapshot.spot_price)}",
        f"{actionability.bias} {actionability.conviction}/3",
        actionability.state_label,
        structure.structure_label,
        f"flip {_format_level(snapshot.gamma_flip)} ({_format_pct(snapshot.dist_to_flip_pct)})",
        f"near {nearest_text}",
    ]
    lines = [" | ".join(parts)]
    lines.append(f"- {actionability.summary}")
    lines.append(f"- Index regime: {actionability.index_regime_summary}")
    lines.append(f"- Risk: {actionability.nearest_risk}")
    lines.append(f"- Structure: {structure.summary}")
    if structure.support_cluster or structure.resistance_cluster:
        lines.append(
            f"- Levels: support {structure.support_cluster or '-'} | resistance {structure.resistance_cluster or '-'}"
        )
        lines.append(
            f"- Density: support {structure.support_density} peaks ({structure.support_peak_count} candidates) | resistance {structure.resistance_density} peaks ({structure.resistance_peak_count} candidates)"
        )
    lines.append(f"- Decision: {decision.summary}")
    lines.append(f"- Trigger: {decision.trigger}")
    lines.append(f"- Invalidation: {decision.invalidation}")
    lines.append(f"- Compare: {comparison.summary}")
    for text in build_trade_implications(snapshot, actionability, structure, decision):
        if text not in {actionability.summary, structure.summary, decision.summary}:
            lines.append(f"- {text}")
    return "\n".join(lines)


def build_enriched_rows(
    result: FastLaneBatchResult,
    *,
    baseline_store: BarchartLevelsHistoryStore | None = None,
    compare_phase: str | None = None,
) -> list[tuple[Snapshot, Actionability, SnapshotComparison]]:
    rows: list[tuple[Snapshot, Actionability, SnapshotComparison, StructureAnalysis, DecisionPlan, RankingEntry]] = []
    for snapshot in result.succeeded:
        structure = build_structure_analysis(snapshot)
        index_regime_bias, index_regime_summary = build_index_regime_bias(snapshot, baseline_store)
        actionability = build_actionability(
            snapshot,
            structure,
            index_regime_bias=index_regime_bias,
            index_regime_summary=index_regime_summary,
        )
        decision = build_decision_plan(snapshot, actionability, structure)
        ranking = build_ranking_entry(snapshot, actionability, structure, decision)
        baseline_row = None
        if baseline_store is not None:
            baseline_row = baseline_store.fetch_latest_baseline(
                snapshot.ticker,
                trading_day=snapshot.trading_day,
                preferred_phase=compare_phase,
            )
        comparison = build_comparison(snapshot, baseline_row)
        rows.append((snapshot, actionability, comparison, structure, decision, ranking))
    return rows


def format_watchlist_ranking(
    enriched_rows: list[tuple[Snapshot, Actionability, SnapshotComparison, StructureAnalysis, DecisionPlan, RankingEntry]]
) -> list[str]:
    if len(enriched_rows) <= 1:
        return []

    rankings = [row[5] for row in enriched_rows]
    top_attention = max(rankings, key=lambda item: item.attention_score)
    top_pin = max(rankings, key=lambda item: item.pin_risk_score)
    top_breakout = max(rankings, key=lambda item: item.breakout_score)
    top_mr = max(rankings, key=lambda item: item.mean_reversion_score)
    ordered = sorted(rankings, key=lambda item: item.attention_score, reverse=True)

    lines = ["Watchlist Ranking"]
    lines.append(f"- Top decision point: {top_attention.ticker} (score {top_attention.attention_score:.2f})")
    lines.append(f"- Highest pin risk: {top_pin.ticker} ({top_pin.pin_risk_score:.2f})")
    lines.append(f"- Best breakout if triggered: {top_breakout.ticker} ({top_breakout.breakout_score:.2f})")
    lines.append(f"- Cleanest mean reversion map: {top_mr.ticker} ({top_mr.mean_reversion_score:.2f})")
    lines.append("- Priority order: " + ", ".join(item.ticker for item in ordered))
    return lines


def format_batch_report(
    result: FastLaneBatchResult,
    *,
    baseline_store: BarchartLevelsHistoryStore | None = None,
    compare_phase: str | None = None,
) -> str:
    enriched_rows = build_enriched_rows(
        result,
        baseline_store=baseline_store,
        compare_phase=compare_phase,
    )
    lines = [f"GEX Fast Lane | batch {result.batch_id} | ok {len(result.succeeded)} | fail {len(result.failed)}"]
    ranking_lines = format_watchlist_ranking(enriched_rows)
    if ranking_lines:
        lines.append("")
        lines.extend(ranking_lines)
    for snapshot, actionability, comparison, structure, decision, ranking in enriched_rows:
        lines.append("")
        lines.append(format_snapshot_compact(snapshot, actionability, comparison, structure, decision))
    if result.failed:
        failed_text = ", ".join(f"{ticker} ({reason})" for ticker, reason in sorted(result.failed.items()))
        lines.append("")
        lines.append(f"Failures: {failed_text}")
    return "\n".join(lines)


def format_batch_report_json(
    result: FastLaneBatchResult,
    *,
    baseline_store: BarchartLevelsHistoryStore | None = None,
    compare_phase: str | None = None,
) -> str:
    enriched_rows = build_enriched_rows(
        result,
        baseline_store=baseline_store,
        compare_phase=compare_phase,
    )
    payload = {
        "batch_id": result.batch_id,
        "ok": len(result.succeeded),
        "fail": len(result.failed),
        "db_path": str(result.db_path),
        "watchlist_ranking": [asdict(row[5]) for row in sorted(enriched_rows, key=lambda item: item[5].attention_score, reverse=True)],
        "results": [
            {
                "snapshot": asdict(snapshot),
                "actionability": asdict(actionability),
                "comparison": asdict(comparison),
                "structure": asdict(structure),
                "decision": asdict(decision),
                "ranking": asdict(ranking),
            }
            for snapshot, actionability, comparison, structure, decision, ranking in enriched_rows
        ],
        "failures": result.failed,
    }
    return json.dumps(payload, indent=2)


def collect_fastlane_batch(
    tickers: Sequence[str],
    *,
    db_path: Path = DEFAULT_FASTLANE_DB_PATH,
    timeout: float = 20.0,
    at_flip_tolerance_pct: float = AT_FLIP_TOLERANCE_PCT,
    trading_day_override: str | None = None,
    captured_at: datetime | None = None,
) -> FastLaneBatchResult:
    batch_time = (captured_at or datetime.now(tz=UTC)).astimezone(UTC)
    batch_id = batch_time.strftime("%Y%m%dT%H%M%SZ")
    succeeded: list[Snapshot] = []
    failed: dict[str, str] = {}
    store = BarchartFastLaneStore(Path(db_path))
    try:
        for raw_ticker in tickers:
            ticker = raw_ticker.strip().upper()
            if not ticker:
                continue
            try:
                snapshot = collect_once(
                    ticker=ticker,
                    session_phase=FASTLANE_SESSION_PHASE,
                    at_flip_tolerance_pct=at_flip_tolerance_pct,
                    timeout=timeout,
                    trading_day_override=trading_day_override,
                )
                snapshot = _as_fastlane_snapshot(snapshot)
                store.insert(snapshot, batch_id=batch_id)
                succeeded.append(snapshot)
            except Exception as exc:  # noqa: BLE001 - continue remaining tickers
                failed[ticker] = str(exc)
    finally:
        store.close()

    return FastLaneBatchResult(
        batch_id=batch_id,
        succeeded=succeeded,
        failed=failed,
        db_path=Path(db_path),
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run compact ad-hoc Barchart GEX batch")
    parser.add_argument("tickers", nargs="+", help="Ticker symbols to fetch")
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_FASTLANE_DB_PATH),
        help="Fast Lane SQLite path (default: state/barchart_fastlane.sqlite)",
    )
    parser.add_argument(
        "--main-db-path",
        default=str(DEFAULT_DB_PATH),
        help="Main collector SQLite path for comparisons (default: state/barchart_levels.sqlite)",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=20.0,
        help="HTTP timeout per ticker in seconds",
    )
    parser.add_argument(
        "--trading-day-override",
        default=None,
        help="Persist this trading day (YYYY-MM-DD) instead of deriving it from current ET date",
    )
    parser.add_argument(
        "--compare-phase",
        default=None,
        help="Prefer this main-session phase (premarket/open/midday/close) for comparison before falling back to latest",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit structured JSON instead of chat-oriented text",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    result = collect_fastlane_batch(
        tickers=args.tickers,
        db_path=Path(args.db_path),
        timeout=args.timeout,
        trading_day_override=args.trading_day_override,
    )
    baseline_store = BarchartLevelsHistoryStore(Path(args.main_db_path))
    try:
        if args.json:
            print(format_batch_report_json(result, baseline_store=baseline_store, compare_phase=args.compare_phase))
        else:
            print(format_batch_report(result, baseline_store=baseline_store, compare_phase=args.compare_phase))
    finally:
        baseline_store.close()
    return 0 if not result.failed else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
