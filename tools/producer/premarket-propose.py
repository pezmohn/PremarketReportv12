#!/usr/bin/env python3
"""Generate a proposal artifact for the pre-market scanner instead of sending directly."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, time
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PRODUCER_DIR = Path(__file__).resolve().parent
PROPOSAL_DIR = WORKSPACE_ROOT / "state/proposals/premarket-scanner"
ACTIVE_DIR = PROPOSAL_DIR / "active"
ARCHIVE_DIR = PROPOSAL_DIR / "archive"
REJECTED_DIR = PROPOSAL_DIR / "rejected"
SCANNER_SCRIPT = PRODUCER_DIR / "premarket-scanner.py"
DEFAULT_TARGET = os.environ.get("PREMARKET_REPORT_TARGET", "telegram:REPLACE_ME")
US_PREMARKET_START_ET = time(9, 0)
US_OPEN_ET = time(9, 30)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

ET_TZ = ZoneInfo("America/New_York") if ZoneInfo else None


def utc_now() -> datetime:
    return datetime.now(UTC)


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


def build_proposal(with_gex: bool, max_age_seconds: int) -> dict:
    now = utc_now()
    proposal_id = now.strftime("%Y-%m-%dT%H%M%SZ")
    phase = current_market_phase(now)
    cmd = [sys.executable, str(SCANNER_SCRIPT), "--meta-json"]
    if with_gex:
        cmd.append("--with-gex")

    meta = {}
    stdout = ""
    stderr = ""
    return_code = 1
    skipped_reason = None
    if phase != "premarket_final":
        skipped_reason = f"wrong_market_phase:{phase}"
    else:
        proc = subprocess.run(cmd, capture_output=True, text=True, cwd=WORKSPACE_ROOT)
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        return_code = proc.returncode
        if stdout:
            try:
                meta = json.loads(stdout)
            except json.JSONDecodeError:
                meta = {}

    proposal = {
        "workflow": "premarket_scanner",
        "proposal_id": proposal_id,
        "created_at": now.isoformat(),
        "proposed_action": {
            "kind": "send_message",
            "target": DEFAULT_TARGET,
            "content": meta.get("message", "") if isinstance(meta, dict) else "",
            "format": "telegram_markdown",
        },
        "evidence": {
            "scanner_command": " ".join(cmd),
            "scanner_exit_code": return_code,
            "stdout_bytes": len(stdout.encode("utf-8", errors="replace")),
            "stderr_bytes": len(stderr.encode("utf-8", errors="replace")),
            "data_timestamp": meta.get("run_finished_at", now.isoformat()) if isinstance(meta, dict) else now.isoformat(),
            "has_fetch_errors": bool(meta.get("has_fetch_errors")) if isinstance(meta, dict) else True,
            "has_stale_flags": bool(meta.get("has_stale_flags")) if isinstance(meta, dict) else True,
            "scanner_ok": bool(meta.get("ok")) if isinstance(meta, dict) else False,
            "candidate_count": int(meta.get("candidate_count", 0)) if isinstance(meta, dict) else 0,
            "scanner_error": skipped_reason or (meta.get("error") if isinstance(meta, dict) else "invalid_meta_output"),
            "stderr_tail": stderr[-1000:],
        },
        "scanner_snapshot": {
            "candidates": meta.get("candidates", []) if isinstance(meta, dict) else [],
            "claims": meta.get("claims", []) if isinstance(meta, dict) else [],
            "report_template": meta.get("report_template") if isinstance(meta, dict) else None,
            "direction_policy": meta.get("direction_policy") if isinstance(meta, dict) else None,
            "trigger_policy": meta.get("trigger_policy") if isinstance(meta, dict) else None,
            "entry_policy": meta.get("entry_policy") if isinstance(meta, dict) else None,
            "gex_overlay": meta.get("gex_overlay") if isinstance(meta, dict) else None,
        },
        "valid_if": {
            "max_age_seconds": max_age_seconds,
            "market_phase": "premarket_final",
            "send_only_if_clean": True,
        },
    }
    return proposal


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate pre-market scanner proposal")
    parser.add_argument("--with-gex", action="store_true")
    parser.add_argument("--max-age-seconds", type=int, default=300)
    args = parser.parse_args()

    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    proposal = build_proposal(with_gex=args.with_gex, max_age_seconds=args.max_age_seconds)
    proposal["status"] = "active"
    path = ACTIVE_DIR / f"{proposal['proposal_id']}.json"
    path.write_text(json.dumps(proposal, indent=2))
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
