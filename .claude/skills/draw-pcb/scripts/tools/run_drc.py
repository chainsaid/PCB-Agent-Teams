#!/usr/bin/env python3
"""draw-pcb toolbox tool: run_drc

Phase D finishing step — run KiCad's DRC and report violation counts by type.
Honours user exclusions set in the .kicad_pro (so triaged violations don't
re-block). This is the deterministic rule check; design-level review belongs
to check-pcb.

Usage:
  run_drc.py <board.kicad_pcb>

Output JSON: {ok, violation_count, unconnected_count, by_type, drc_json,
warnings?}.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Tried newest-first — matches the vendored KiCadRoutingTools/install_plugin.py
# precedent so a KiCad 9.x install isn't left unsupported by an assumed-10.0 path.
_KICAD_WINDOWS_VERSIONS = ["10.0", "9.99", "9.0"]


def _windows_kicad_cli_candidates() -> list:
    """KiCad's Windows installer lets the user pick any drive, so scan them
    all rather than assuming C: (e.g. `D:\\Program Files\\KiCad\\10.0\\...`).
    Checks %ProgramFiles%/%ProgramFiles(x86)%/%ProgramW6432% first (the
    common case — avoids a 26-drive stat sweep, including any
    offline/disconnected mapped drives, on every call)."""
    exe_name = "kicad-cli.exe"
    roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    fast = [f"{r}\\KiCad\\{ver}\\bin\\{exe_name}" for r in roots if r for ver in _KICAD_WINDOWS_VERSIONS]
    subpaths = [rf"Program Files\KiCad\{ver}\bin\{exe_name}" for ver in _KICAD_WINDOWS_VERSIONS] + \
               [rf"Program Files (x86)\KiCad\{ver}\bin\{exe_name}" for ver in _KICAD_WINDOWS_VERSIONS]
    scan = [f"{d}:\\{sub}" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" for sub in subpaths]
    return fast + scan


_KICAD_CLI_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/snap/kicad/current/bin/kicad-cli",
] + _windows_kicad_cli_candidates()

# drc_exclusions lives one level up in scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _find_kicad_cli() -> str | None:
    import shutil
    for p in _KICAD_CLI_CANDIDATES:
        if Path(p).exists():
            return p
    return shutil.which("kicad-cli")


def run_drc(pcb_path: str) -> dict:
    cli = _find_kicad_cli()
    if not cli:
        return {"ok": False, "error": "kicad-cli not found"}

    # kicad-cli resolves design rules by board filename. With no same-named
    # project file it uses KiCad DEFAULTS without saying so, and every
    # violation is then measured against rules this board never had.
    warnings: list[str] = []
    pro = Path(pcb_path).with_suffix(".kicad_pro")
    if not pro.exists():
        warnings.append(
            f"no {pro.name} beside the board — DRC ran on KiCad DEFAULT design "
            "rules, not this board's. Expect fake drill / annular / clearance "
            "violations. Copy the project file next to the board and re-run.")

    drc_file = Path(pcb_path).with_suffix(".drc.json")
    try:
        subprocess.run([cli, "pcb", "drc", "--format", "json",
                        "-o", str(drc_file), pcb_path],
                       capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "DRC timed out"}
    if not drc_file.exists():
        return {"ok": False, "error": "DRC produced no output"}

    data = json.loads(drc_file.read_text())

    # Honour .kicad_pro exclusions — triaged items stay in JSON for trace but
    # don't drive the count.
    try:
        from drc_exclusions import read_exclusions_from_pro, prune
        prune(data, read_exclusions_from_pro(pcb_path))
    except ImportError:
        pass

    violations = data.get("violations", []) or []
    unconnected = data.get("unconnected_items", []) or []
    by_type: dict[str, int] = {}
    for x in violations + unconnected:
        t = x.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1

    result = {
        "ok": True,
        "violation_count": len(violations),
        "unconnected_count": len(unconnected),
        "by_type": by_type,
        "drc_json": str(drc_file),
    }
    if warnings:
        result["warnings"] = warnings
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="Run KiCad DRC")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    args = ap.parse_args()
    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1
    result = run_drc(args.pcb)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
