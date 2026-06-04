#!/usr/bin/env python3
"""Verify whether a pre-market scanner proposal is still valid to apply."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, time
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROPOSAL_DIR = WORKSPACE_ROOT / "state/proposals/premarket-scanner"
ACTIVE_DIR = PROPOSAL_DIR / "active"
ARCHIVE_DIR = PROPOSAL_DIR / "archive"
REJECTED_DIR = PROPOSAL_DIR / "rejected"
US_PREMARKET_START_ET = time(9, 0)
US_OPEN_ET = time(9, 30)
ET_TZ = ZoneInfo("America/New_York") if ZoneInfo else None


def current_market_phase(now_utc: datetime) -> str:
    if ET_TZ is None:
        return "unknown"
    eastern = now_utc.astimezone(ET_TZ)
    t = eastern.time()
    if US_PREMARKET_START_ET <= t < US_OPEN_ET:
        return "premarket_final"
    if t < US_OPEN_ET:
        return "early_premarket"
    return "post_open"


def load_latest() -> Path:
    candidates = sorted(ACTIVE_DIR.glob("*.json"))
    if not candidates:
        raise FileNotFoundError("No active premarket proposal files found")
    return candidates[-1]


def _write_status(path: Path, proposal: dict, status: str, reason: str | None = None) -> Path:
    proposal["status"] = status
    proposal["status_updated_at"] = datetime.now(UTC).isoformat()
    if reason is not None:
        proposal["status_reason"] = reason
    target_dir = ACTIVE_DIR if status == "active" else ARCHIVE_DIR if status == "applied" else REJECTED_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    target.write_text(json.dumps(proposal, indent=2))
    if path != target and path.exists():
        path.unlink()
    return target


def verify(path: Path, *, update_status: bool = False) -> dict:
    proposal = json.loads(path.read_text())
    now = datetime.now(UTC)
    created_at = datetime.fromisoformat(proposal["created_at"])
    age_seconds = (now - created_at).total_seconds()
    max_age_seconds = proposal.get("valid_if", {}).get("max_age_seconds", 300)
    evidence = proposal.get("evidence", {})
    content = proposal.get("proposed_action", {}).get("content", "")

    result = None
    if age_seconds > max_age_seconds:
        result = {"ok": False, "reason": "proposal_expired", "age_seconds": age_seconds}
    elif current_market_phase(now) != proposal.get("valid_if", {}).get("market_phase"):
        result = {"ok": False, "reason": "market_phase_mismatch", "age_seconds": age_seconds}
    elif evidence.get("scanner_exit_code") != 0 or not evidence.get("scanner_ok", False):
        result = {"ok": False, "reason": "scanner_failed", "age_seconds": age_seconds}
    elif evidence.get("has_fetch_errors"):
        result = {"ok": False, "reason": "fetch_errors", "age_seconds": age_seconds}
    elif evidence.get("has_stale_flags"):
        result = {"ok": False, "reason": "stale_or_unreliable", "age_seconds": age_seconds}
    elif not content or not content.strip():
        result = {"ok": False, "reason": "empty_content", "age_seconds": age_seconds}
    else:
        result = {"ok": True, "reason": "verified", "age_seconds": age_seconds}

    if update_status and not result["ok"]:
        _write_status(path, proposal, "rejected", result["reason"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify pre-market scanner proposal")
    parser.add_argument("path", nargs="?", help="Proposal JSON path")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()

    if args.latest or not args.path:
        path = load_latest()
    else:
        path = Path(args.path)

    result = verify(path, update_status=True)
    print(json.dumps(result))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
