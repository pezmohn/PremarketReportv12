#!/usr/bin/env python3
"""Validate saved pre-market scanner reports against regular-session outcomes.

V1 goal: calibration, not a full execution backtest. The script answers:
- did the stated side have follow-through?
- were trigger levels reached and useful?
- did warnings identify noisy/choppy candidates?
- did higher tradeability grades rank better than the rest?
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROPOSAL_DIR = WORKSPACE_ROOT / "state/proposals/premarket-scanner"
ARCHIVE_DIR = PROPOSAL_DIR / "archive"
ACTIVE_DIR = PROPOSAL_DIR / "active"
OUTPUT_DIR = WORKSPACE_ROOT / "state/validation/premarket-scanner"
CACHE_DIR = WORKSPACE_ROOT / "scripts/data/premarket_eod_cache"
LEVEL_CACHE_DIR = WORKSPACE_ROOT / "scripts/data/level_backtest_cache"
API_KEY_PATH = WORKSPACE_ROOT / "sync/polygon-api-key.txt"
BASE_URL = "https://api.polygon.io"
ET_TZ = ZoneInfo("America/New_York") if ZoneInfo else None

RATE_LIMIT = 4.5
_last_req = 0.0


@dataclass
class CandidateClaim:
    ticker: str
    pm_price: float
    scanner_score: int | None = None
    pm_change_pct: float | None = None
    expected_side: str = "neutral"
    tradeability_grade: str | None = None
    tradeability_read: str | None = None
    trigger_price: float | None = None
    trigger_name: str | None = None
    setup_type: str = "S1/S2"
    warnings: list[str] | None = None


def _api_key() -> str:
    return API_KEY_PATH.read_text().strip()


def _get(url: str, params: dict[str, Any]) -> dict[str, Any]:
    global _last_req
    wait = 1.0 / RATE_LIMIT - (time.time() - _last_req)
    if wait > 0:
        time.sleep(wait)
    resp = requests.get(url, params=params, timeout=20)
    _last_req = time.time()
    resp.raise_for_status()
    return resp.json()


def proposal_trading_day(proposal: dict[str, Any]) -> str:
    created = datetime.fromisoformat(proposal["created_at"])
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    if ET_TZ is None:
        return created.date().isoformat()
    return created.astimezone(ET_TZ).date().isoformat()


def load_latest_applied() -> Path:
    candidates = sorted(ARCHIVE_DIR.glob("*.json"))
    applied = []
    for path in candidates:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if data.get("status") == "applied":
            applied.append(path)
    if not applied:
        raise FileNotFoundError("No applied premarket scanner proposal found")
    return applied[-1]


def load_proposals_for_date(day: str, proposal_dir: Path = PROPOSAL_DIR) -> list[Path]:
    archive_dir = proposal_dir / "archive"
    active_dir = proposal_dir / "active"
    paths = sorted([*archive_dir.glob("*.json"), *active_dir.glob("*.json")])
    matches = []
    for path in paths:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        if proposal_trading_day(data) == day and data.get("status") == "applied":
            matches.append(path)
    return matches


def _grade_rank(grade: str | None) -> int:
    if not grade:
        return 0
    base = grade[0].upper()
    rank = {"A": 4, "B": 3, "C": 2, "D": 1}.get(base, 0)
    if "+" in grade:
        rank += 1
    if "-" in grade:
        rank -= 1
    return rank


def _side_from_text(text: str) -> str:
    if "SHORT SIDE" in text:
        return "short"
    if "LONG SIDE" in text:
        return "long"
    return "neutral"


def extract_claims_from_snapshot(proposal: dict[str, Any]) -> list[CandidateClaim]:
    snapshot = proposal.get("scanner_snapshot") or {}
    replay_claims = snapshot.get("claims") or []
    if replay_claims:
        claims = []
        for item in replay_claims:
            if not item.get("ticker"):
                continue
            claims.append(
                CandidateClaim(
                    ticker=item["ticker"],
                    pm_price=float(item.get("pm_price") or 0),
                    scanner_score=item.get("scanner_score"),
                    pm_change_pct=item.get("pm_change_pct"),
                    expected_side=item.get("expected_side") or "neutral",
                    tradeability_grade=item.get("tradeability_grade"),
                    tradeability_read=item.get("tradeability_read"),
                    trigger_price=item.get("trigger_price"),
                    trigger_name=item.get("trigger_name"),
                    setup_type=item.get("setup_type") or "S1/S2",
                    warnings=item.get("warnings") or [],
                )
            )
        return claims

    candidates = snapshot.get("candidates") or []
    if not candidates:
        return []
    gex_overlay = snapshot.get("gex_overlay") or {}
    claims = []
    for item in candidates:
        ticker = item.get("ticker")
        if not ticker:
            continue
        gex = gex_overlay.get(ticker) if isinstance(gex_overlay, dict) else None
        warnings = []
        if item.get("narrow_pm"):
            warnings.append("narrow_pm")
        if gex and gex.get("gamma_box"):
            warnings.append("gamma_box")
        gf1 = item.get("gapfade1")
        claim = CandidateClaim(
            ticker=ticker,
            pm_price=float(item.get("pm_price") or 0),
            scanner_score=item.get("score"),
            pm_change_pct=item.get("pm_chg_pct"),
            expected_side=item.get("range_bias") or "neutral",
            trigger_price=_safe_float(gex.get("trigger")) if gex else None,
            setup_type="GapFade1" if gf1 else "S1/S2",
            warnings=warnings,
        )
        if claim.setup_type == "GapFade1":
            claim.expected_side = "short"
        claims.append(claim)
    return claims


TOP_LINE_RE = re.compile(
    r"^\d+\)\s+\*\*(?P<ticker>[A-Z][A-Z0-9.]*)\*\*\s+\|\s+(?P<grade>[A-C][+-]?)\s+\|\s+Scanner\s+(?P<score>\d+)\s+\|\s+(?P<read>.*?)\s+\|\s+Trigger\s+(?P<trigger>[0-9.]+|-)"
)
TICKER_LINE_RE = re.compile(
    r"\*\*(?P<ticker>[A-Z][A-Z0-9.]*)\*\*\s+(?P<chg>[+-]?[0-9.]+)%\s+.*?\$(?P<price>[0-9.]+)\s+\|\s+Score:\s+(?P<score>\d+)/100"
)
TRIGGER_RE = re.compile(r"Trigger:\s+(?P<trigger>[0-9.]+)")
GF1_RE = re.compile(r"\*\*(?P<ticker>[A-Z][A-Z0-9.]*)\*\*\s+Gap\s+\+(?P<gap>[0-9.]+)%")


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"[0-9]+(?:\.[0-9]+)?", str(value))
    return float(match.group(0)) if match else None


def extract_claims_from_text(proposal: dict[str, Any]) -> list[CandidateClaim]:
    content = proposal.get("proposed_action", {}).get("content", "")
    top_meta: dict[str, dict[str, Any]] = {}
    gf1_tickers: set[str] = set()
    for line in content.splitlines():
        top = TOP_LINE_RE.search(line.strip())
        if top:
            top_meta[top.group("ticker")] = {
                "grade": top.group("grade"),
                "read": top.group("read"),
                "trigger": _safe_float(top.group("trigger")),
            }
        gf1 = GF1_RE.search(line)
        if gf1:
            gf1_tickers.add(gf1.group("ticker"))

    claims: list[CandidateClaim] = []
    current: CandidateClaim | None = None
    for raw_line in content.splitlines():
        line = raw_line.strip()
        ticker_line = TICKER_LINE_RE.search(line)
        if ticker_line:
            if current:
                claims.append(current)
            ticker = ticker_line.group("ticker")
            meta = top_meta.get(ticker, {})
            current = CandidateClaim(
                ticker=ticker,
                pm_price=float(ticker_line.group("price")),
                scanner_score=int(ticker_line.group("score")),
                pm_change_pct=float(ticker_line.group("chg")),
                tradeability_grade=meta.get("grade"),
                tradeability_read=meta.get("read"),
                trigger_price=meta.get("trigger"),
                setup_type="GapFade1" if ticker in gf1_tickers else "S1/S2",
                warnings=[],
            )
            if current.setup_type == "GapFade1":
                current.expected_side = "short"
            continue
        if not current:
            continue
        side = _side_from_text(line)
        if side != "neutral" and current.setup_type != "GapFade1":
            current.expected_side = side
        if "PM Range eng" in line:
            current.warnings = [*(current.warnings or []), "narrow_pm"]
        if "boxed" in line or "gamma box" in line.lower():
            current.warnings = [*(current.warnings or []), "gamma_box"]
        trigger = TRIGGER_RE.search(line)
        if trigger and current.trigger_price is None:
            current.trigger_price = float(trigger.group("trigger"))
    if current:
        claims.append(current)
    return claims


def extract_claims(proposal: dict[str, Any]) -> list[CandidateClaim]:
    claims = extract_claims_from_snapshot(proposal)
    return claims if claims else extract_claims_from_text(proposal)


def fetch_1min_bars(ticker: str, day: str, api_key: str) -> list[dict[str, Any]]:
    shared_cache_path = LEVEL_CACHE_DIR / f"{ticker}_{day}_1min.json"
    if shared_cache_path.exists():
        return json.loads(shared_cache_path.read_text())

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = CACHE_DIR / f"{ticker}_{day}_1min.json"
    if cache_path.exists():
        return json.loads(cache_path.read_text())
    data = _get(
        f"{BASE_URL}/v2/aggs/ticker/{ticker}/range/1/minute/{day}/{day}",
        {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key},
    )
    bars = data.get("results", [])
    cache_path.write_text(json.dumps(bars))
    return bars


def regular_session_bars(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if ET_TZ is None:
        return bars
    rth = []
    for bar in bars:
        ts = datetime.fromtimestamp(bar["t"] / 1000, tz=UTC).astimezone(ET_TZ)
        if ts.hour > 9 or (ts.hour == 9 and ts.minute >= 30):
            if ts.hour < 16:
                rth.append(bar)
    return rth


def first_trigger_index(bars: list[dict[str, Any]], trigger_price: float | None) -> int | None:
    if trigger_price is None:
        return None
    for i, bar in enumerate(bars):
        if bar.get("l", 0) <= trigger_price <= bar.get("h", 0):
            return i
    return None


def evaluate_claim(claim: CandidateClaim, bars: list[dict[str, Any]]) -> dict[str, Any]:
    rth = regular_session_bars(bars)
    if not rth:
        return {"ticker": claim.ticker, "ok": False, "reason": "no_rth_bars", "claim": asdict(claim)}

    open_price = float(rth[0]["o"])
    close_price = float(rth[-1]["c"])
    high_price = max(float(b["h"]) for b in rth)
    low_price = min(float(b["l"]) for b in rth)
    day_return_pct = (close_price - open_price) / open_price * 100

    if claim.expected_side == "short":
        aligned = day_return_pct < 0
        mfe_pct = (open_price - low_price) / open_price * 100
        mae_pct = (high_price - open_price) / open_price * 100
    elif claim.expected_side == "long":
        aligned = day_return_pct > 0
        mfe_pct = (high_price - open_price) / open_price * 100
        mae_pct = (open_price - low_price) / open_price * 100
    else:
        aligned = abs(day_return_pct) < 1.0
        mfe_pct = abs(day_return_pct)
        mae_pct = 0.0

    trigger_idx = first_trigger_index(rth, claim.trigger_price)
    trigger_touched = trigger_idx is not None
    trigger_followthrough_pct = None
    if trigger_idx is not None and claim.expected_side != "neutral":
        after = rth[trigger_idx:]
        trigger = claim.trigger_price or open_price
        if claim.expected_side == "long":
            trigger_followthrough_pct = (max(float(b["h"]) for b in after) - trigger) / trigger * 100
        else:
            trigger_followthrough_pct = (trigger - min(float(b["l"]) for b in after)) / trigger * 100

    warned = bool(claim.warnings)
    choppy_or_failed = abs(day_return_pct) < 0.75 or not aligned or mae_pct > mfe_pct

    return {
        "ticker": claim.ticker,
        "ok": True,
        "claim": asdict(claim),
        "open": round(open_price, 4),
        "close": round(close_price, 4),
        "high": round(high_price, 4),
        "low": round(low_price, 4),
        "day_return_pct": round(day_return_pct, 2),
        "aligned_with_side": aligned,
        "mfe_pct": round(mfe_pct, 2),
        "mae_pct": round(mae_pct, 2),
        "trigger_touched": trigger_touched,
        "trigger_followthrough_pct": None if trigger_followthrough_pct is None else round(trigger_followthrough_pct, 2),
        "warning_helped": warned and choppy_or_failed,
        "grade_rank": _grade_rank(claim.tradeability_grade),
    }


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def score_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    valid = [r for r in results if r.get("ok")]
    if not valid:
        return {"score": 0, "reason": "no_valid_results"}

    direction_accuracy = _mean([1.0 if r["aligned_with_side"] else 0.0 for r in valid])
    avg_mfe = _mean([float(r["mfe_pct"]) for r in valid])
    trigger_candidates = [r for r in valid if r["claim"].get("trigger_price")]
    trigger_success = _mean(
        [
            1.0
            if r.get("trigger_touched") and (r.get("trigger_followthrough_pct") or 0) >= 0.5
            else 0.0
            for r in trigger_candidates
        ]
    ) if trigger_candidates else 0.0
    warning_candidates = [r for r in valid if r["claim"].get("warnings")]
    warning_accuracy = _mean([1.0 if r.get("warning_helped") else 0.0 for r in warning_candidates]) if warning_candidates else 0.0

    ranked = [r for r in valid if r.get("grade_rank", 0) > 0]
    top = [r for r in ranked if r["grade_rank"] >= 3]
    lower = [r for r in valid if r not in top]
    top_mfe = _mean([float(r["mfe_pct"]) for r in top])
    lower_mfe = _mean([float(r["mfe_pct"]) for r in lower])
    grade_alignment = 1.0 if not top else max(0.0, min(1.0, (top_mfe - lower_mfe + 1.0) / 2.0))

    score = (
        25 * direction_accuracy
        + 20 * trigger_success
        + 20 * warning_accuracy
        + 20 * min(avg_mfe / 2.0, 1.0)
        + 15 * grade_alignment
    )
    return {
        "score": round(score),
        "direction_accuracy_pct": round(direction_accuracy * 100, 1),
        "trigger_success_pct": round(trigger_success * 100, 1),
        "warning_accuracy_pct": round(warning_accuracy * 100, 1),
        "avg_mfe_pct": round(avg_mfe, 2),
        "top_grade_mfe_pct": round(top_mfe, 2),
        "lower_grade_mfe_pct": round(lower_mfe, 2),
        "valid_results": len(valid),
    }


def build_summary(day: str, scoring: dict[str, Any], results: list[dict[str, Any]]) -> str:
    if scoring.get("reason") == "no_candidate_claims":
        return "\n".join(
            [
                f"Premarket Scanner EOD — {day}",
                "Score: n/a | No candidates in applied premarket proposal",
                "",
                "No validation run needed. The scanner produced an empty applied report, so this day is excluded from champion/challenger scoring.",
            ]
        )

    valid = [r for r in results if r.get("ok")]
    best = sorted(valid, key=lambda r: r.get("mfe_pct", 0), reverse=True)[:3]
    misses = sorted(valid, key=lambda r: r.get("mae_pct", 0), reverse=True)[:3]
    lines = [
        f"Premarket Scanner EOD — {day}",
        f"Score: {scoring.get('score', 0)}/100 | Direction {scoring.get('direction_accuracy_pct', 0)}% | Trigger {scoring.get('trigger_success_pct', 0)}%",
        f"Avg MFE: {scoring.get('avg_mfe_pct', 0)}% | Warnings useful: {scoring.get('warning_accuracy_pct', 0)}%",
        "",
        "Best follow-through:",
    ]
    for r in best:
        lines.append(f"- {r['ticker']}: {r['claim']['expected_side']} | MFE {r['mfe_pct']}% | close {r['day_return_pct']}%")
    lines.append("")
    lines.append("Worst adverse excursion:")
    for r in misses:
        lines.append(f"- {r['ticker']}: {r['claim']['expected_side']} | MAE {r['mae_pct']}% | close {r['day_return_pct']}%")
    return "\n".join(lines)


def validate_proposal(path: Path, *, api_key: str | None = None) -> dict[str, Any]:
    proposal = json.loads(path.read_text())
    day = proposal_trading_day(proposal)
    claims = extract_claims(proposal)
    if not claims:
        scoring = {
            "score": None,
            "reason": "no_candidate_claims",
            "valid_results": 0,
        }
        return {
            "workflow": "premarket_scanner_eod_validation",
            "variant": proposal.get("variant", "v1"),
            "proposal_path": str(path),
            "trading_day": day,
            "generated_at": datetime.now(UTC).isoformat(),
            "scorable": False,
            "scoring": scoring,
            "results": [],
            "summary": build_summary(day, scoring, []),
        }
    key = api_key or _api_key()
    results = []
    for claim in claims:
        bars = fetch_1min_bars(claim.ticker, day, key)
        results.append(evaluate_claim(claim, bars))
    scoring = score_results(results)
    return {
        "workflow": "premarket_scanner_eod_validation",
        "variant": proposal.get("variant", "v1"),
        "proposal_path": str(path),
        "trading_day": day,
        "generated_at": datetime.now(UTC).isoformat(),
        "scorable": True,
        "scoring": scoring,
        "results": results,
        "summary": build_summary(day, scoring, results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate pre-market scanner report against EOD outcome")
    parser.add_argument("proposal", nargs="?", help="Proposal JSON path")
    parser.add_argument("--latest", action="store_true", help="Use latest applied proposal")
    parser.add_argument("--date", help="Validate all applied proposals for YYYY-MM-DD")
    parser.add_argument("--proposal-dir", default=str(PROPOSAL_DIR))
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    if args.date:
        paths = load_proposals_for_date(args.date, Path(args.proposal_dir))
        if not paths:
            raise SystemExit(f"No applied proposal found for {args.date}")
    elif args.latest or not args.proposal:
        paths = [load_latest_applied()]
    else:
        paths = [Path(args.proposal)]

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for path in paths:
        report = validate_proposal(path)
        out = output_dir / f"{report['trading_day']}.json"
        out.write_text(json.dumps(report, indent=2, default=str))
        md = output_dir / f"{report['trading_day']}.md"
        md.write_text(report["summary"] + "\n")
        written.append(out)
        if args.print_summary:
            print(report["summary"])
    if not args.print_summary:
        print("\n".join(str(path) for path in written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
