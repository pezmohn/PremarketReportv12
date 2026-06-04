#!/usr/bin/env python3
"""Apply a verified pre-market scanner proposal by printing the candidate message."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PRODUCER_DIR = Path(__file__).resolve().parent
PROPOSAL_DIR = WORKSPACE_ROOT / "state/proposals/premarket-scanner"
ACTIVE_DIR = PROPOSAL_DIR / "active"
ARCHIVE_DIR = PROPOSAL_DIR / "archive"
VERIFY_SCRIPT = PRODUCER_DIR / "premarket-verify.py"


def load_latest() -> Path:
    candidates = sorted(ACTIVE_DIR.glob("*.json"))
    if not candidates:
        raise FileNotFoundError("No active premarket proposal files found")
    return candidates[-1]


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply pre-market proposal if verify passes")
    parser.add_argument("path", nargs="?", help="Proposal JSON path")
    parser.add_argument("--latest", action="store_true")
    args = parser.parse_args()

    if args.latest or not args.path:
        path = load_latest()
    else:
        path = Path(args.path)

    verify = subprocess.run([sys.executable, str(VERIFY_SCRIPT), str(path)], capture_output=True, text=True, cwd=WORKSPACE_ROOT)
    if verify.returncode != 0:
        sys.stderr.write(verify.stdout or verify.stderr)
        return verify.returncode

    proposal = json.loads(path.read_text())
    proposal["status"] = "applied"
    proposal["status_updated_at"] = __import__('datetime').datetime.now(__import__('datetime').UTC).isoformat()
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    archived = ARCHIVE_DIR / path.name
    archived.write_text(json.dumps(proposal, indent=2))
    if path.exists():
        path.unlink()
    print(proposal["proposed_action"]["content"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
