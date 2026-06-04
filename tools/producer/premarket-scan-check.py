#!/usr/bin/env python3
"""Deterministic Pre-Market Scanner wrapper for OpenClaw crons.

Runs the existing Proposal -> Verify -> Apply flow with bounded subprocess
timeouts and emits exactly one cron-friendly result:

- the Telegram-ready scanner message when verification succeeds
- NO_REPLY when the workflow fails closed
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PRODUCER_DIR = Path(__file__).resolve().parent
PROPOSE_SCRIPT = PRODUCER_DIR / "premarket-propose.py"
APPLY_SCRIPT = PRODUCER_DIR / "premarket-apply.py"


def run_command(args: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            args,
            cwd=WORKSPACE_ROOT,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Run pre-market scanner Proposal -> Verify -> Apply")
    parser.add_argument("--with-gex", action="store_true")
    parser.add_argument("--propose-timeout", type=int, default=150)
    parser.add_argument("--apply-timeout", type=int, default=30)
    args = parser.parse_args()

    propose_cmd = [sys.executable, str(PROPOSE_SCRIPT)]
    if args.with_gex:
        propose_cmd.append("--with-gex")

    proposal = run_command(propose_cmd, args.propose_timeout)
    if proposal is None or proposal.returncode != 0:
        print("NO_REPLY")
        return 0

    proposal_path = proposal.stdout.strip().splitlines()[-1] if proposal.stdout.strip() else ""
    if not proposal_path:
        print("NO_REPLY")
        return 0

    apply = run_command([sys.executable, str(APPLY_SCRIPT), proposal_path], args.apply_timeout)
    if apply is None or apply.returncode != 0:
        print("NO_REPLY")
        return 0

    content = apply.stdout.strip()
    print(content if content else "NO_REPLY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
