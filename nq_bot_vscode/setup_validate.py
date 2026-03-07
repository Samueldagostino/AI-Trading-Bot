#!/usr/bin/env python3
"""
NQ Trading Bot — Project Setup Validator
==========================================
Run this after placing CLAUDE.md in your project root.
It discovers your folder structure, validates key files exist,
and reports what Claude Code Agent Teams will see.

Usage:
  python setup_validate.py              # Run from project root
  python setup_validate.py /path/to/bot # Or specify the path
"""

import os
import sys
import ast
from pathlib import Path

# ── Expected files (relative to project root) ────────────────────────
CRITICAL_FILES = [
    "main.py",
    "config/settings.py",
    "execution/scale_out_executor.py",
    "risk/engine.py",
    "signals/aggregator.py",
    "features/engine.py",
    "features/htf_engine.py",
]

OPTIONAL_FILES = [
    "CLAUDE.md",
    "risk/regime_detector.py",
    "monitoring/engine.py",
    "data_pipeline/pipeline.py",
    "database/connection.py",
    "broker/tradovate_client.py",
    "dashboard/server.py",
    "scripts/run_backtest.py",
]

HC_CONSTANTS = [
    "HIGH_CONVICTION_MIN_SCORE",
    "HIGH_CONVICTION_MAX_STOP_PTS",
    "HIGH_CONVICTION_TP1_RR_RATIO",
]


def find_project_root(start_path: str = ".") -> Path:
    """Walk up from start_path looking for main.py with TradingOrchestrator."""
    p = Path(start_path).resolve()
    
    # Check current dir first
    if (p / "main.py").exists():
        return p
    
    # Search subdirectories (1 level deep)
    for child in p.iterdir():
        if child.is_dir() and (child / "main.py").exists():
            return child
    
    # Search common project locations
    common = [
        Path.home() / "nq-trading-bot",
        Path.home() / "trading-bot",
        Path.home() / "Projects" / "nq-trading-bot",
        Path.home() / "Desktop" / "nq-trading-bot",
        Path.home() / "Documents" / "nq-trading-bot",
    ]
    for loc in common:
        if loc.exists() and (loc / "main.py").exists():
            return loc
    
    return p  # Fall back to start


def check_file(root: Path, rel_path: str) -> dict:
    """Check if a file exists and is valid Python."""
    full = root / rel_path
    result = {"path": rel_path, "exists": full.exists(), "syntax_ok": None, "lines": 0}
    
    if full.exists() and rel_path.endswith(".py"):
        try:
            source = full.read_text()
            ast.parse(source)
            result["syntax_ok"] = True
            result["lines"] = len(source.splitlines())
        except SyntaxError as e:
            result["syntax_ok"] = False
            result["error"] = str(e)
    
    return result


def check_hc_filter(root: Path) -> dict:
    """Verify HC filter constants exist in main.py."""
    main_path = root / "main.py"
    if not main_path.exists():
        return {"found": False, "error": "main.py not found"}
    
    source = main_path.read_text()
    found = {}
    for const in HC_CONSTANTS:
        found[const] = const in source
    
    # Check for c1_target_override in scale_out_executor
    executor_path = root / "execution" / "scale_out_executor.py"
    if executor_path.exists():
        exec_source = executor_path.read_text()
        found["c1_target_override"] = "c1_target_override" in exec_source
    
    return found


def discover_structure(root: Path) -> list:
    """Find all Python files in the project."""
    py_files = []
    for f in sorted(root.rglob("*.py")):
        if "__pycache__" in str(f) or "node_modules" in str(f) or ".venv" in str(f):
            continue
        rel = f.relative_to(root)
        py_files.append(str(rel))
    return py_files


def main():
    start = sys.argv[1] if len(sys.argv) > 1 else "."
    root = find_project_root(start)
    
    print("=" * 65)
    print("  NQ TRADING BOT — PROJECT VALIDATOR")
    print("=" * 65)
    print(f"  Project root: {root}")
    print()
    
    # ── 1. Check CLAUDE.md ────────────────────────────────────────
    claude_md = root / "CLAUDE.md"
    if claude_md.exists():
        lines = len(claude_md.read_text().splitlines())
        print(f"  [OK] CLAUDE.md found ({lines} lines)")
    else:
        print(f"  [!!] CLAUDE.md NOT FOUND — copy it to: {root}/CLAUDE.md")
        print(f"       Agent Teams need this file for shared context.")
    print()
    
    # ── 2. Critical files ─────────────────────────────────────────
    print("  CRITICAL FILES:")
    all_critical = True
    for f in CRITICAL_FILES:
        result = check_file(root, f)
        if result["exists"]:
            status = "OK" if result["syntax_ok"] else f"SYNTAX ERROR: {result.get('error','')}"
            print(f"    [OK] {f} ({result['lines']} lines) — {status}")
        else:
            print(f"    [!!] {f} — MISSING")
            all_critical = False
    print()
    
    # ── 3. Optional files ─────────────────────────────────────────
    print("  OPTIONAL FILES:")
    for f in OPTIONAL_FILES:
        if f == "CLAUDE.md":
            continue
        result = check_file(root, f)
        if result["exists"]:
            print(f"    [OK] {f}")
        else:
            print(f"    [--] {f} (not found, may be fine)")
    print()
    
    # ── 4. HC Filter validation ───────────────────────────────────
    print("  HIGH-CONVICTION FILTER:")
    hc = check_hc_filter(root)
    for key, present in hc.items():
        status = "OK" if present else "MISSING"
        icon = "OK" if present else "!!"
        print(f"    [{icon}] {key}: {status}")
    print()
    
    # ── 5. Full file discovery ────────────────────────────────────
    all_py = discover_structure(root)
    print(f"  ALL PYTHON FILES ({len(all_py)} found):")
    for f in all_py:
        print(f"    {f}")
    print()
    
    # ── 6. Summary ────────────────────────────────────────────────
    print("=" * 65)
    if all_critical and claude_md.exists() and all(hc.values()):
        print("  STATUS: READY FOR CLAUDE CODE AGENT TEAMS")
        print()
        print("  Next steps:")
        print("    1. cd " + str(root))
        print("    2. Enable agent teams:")
        print("       export CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1")
        print("    3. Launch Claude Code:")
        print("       claude")
        print("    4. Tell the lead what you want built.")
    else:
        print("  STATUS: ISSUES FOUND — fix items marked [!!] above")
        if not claude_md.exists():
            print(f"  → Copy CLAUDE.md to {root}/")
        if not all_critical:
            print("  → Missing critical files — check your project path")
    print("=" * 65)


if __name__ == "__main__":
    main()
