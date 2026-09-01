"""Shared helper for draw-pcb toolbox tools that mutate the board.

Board mutation needs the `pcbnew` module, which only exists inside KiCad's
bundled Python (not the workspace .venv). This module locates that interpreter
and invokes _kicad_python_helper.py as a subprocess.
"""
import json
import os
import subprocess
import tempfile
from pathlib import Path

_HELPER = Path(__file__).resolve().parent.parent / "_kicad_python_helper.py"

# Tried newest-first — matches the vendored KiCadRoutingTools/install_plugin.py
# precedent so a KiCad 9.x install isn't left unsupported by an assumed-10.0 path.
_KICAD_WINDOWS_VERSIONS = ["10.0", "9.99", "9.0"]


def _windows_kicad_candidates() -> list[str]:
    """KiCad's Windows installer lets the user pick any drive, so scan them
    all rather than assuming C: (e.g. `D:\\Program Files\\KiCad\\10.0\\...`).
    Checks %ProgramFiles%/%ProgramFiles(x86)%/%ProgramW6432% first (the
    common case — avoids a 26-drive stat sweep, including any
    offline/disconnected mapped drives, on every call)."""
    exe_name = "python.exe"
    roots = [os.environ.get(v) for v in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432")]
    fast = [f"{r}\\KiCad\\{ver}\\bin\\{exe_name}" for r in roots if r for ver in _KICAD_WINDOWS_VERSIONS]
    subpaths = [rf"Program Files\KiCad\{ver}\bin\{exe_name}" for ver in _KICAD_WINDOWS_VERSIONS] + \
               [rf"Program Files (x86)\KiCad\{ver}\bin\{exe_name}" for ver in _KICAD_WINDOWS_VERSIONS]
    scan = [f"{d}:\\{sub}" for d in "CDEFGHIJKLMNOPQRSTUVWXYZ" for sub in subpaths]
    return fast + scan


_KICAD_PYTHON_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/Current/bin/python3",
    "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/"
    "Versions/3.9/bin/python3.9",
    "/usr/lib/kicad/python3",
] + _windows_kicad_candidates()


def find_kicad_python() -> str | None:
    for p in _KICAD_PYTHON_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def call_helper(spec: dict, timeout: int = 120) -> dict:
    """Run _kicad_python_helper.py with `spec` and return its JSON result."""
    kp = find_kicad_python()
    if not kp:
        return {"ok": False,
                "error": "KiCad bundled Python not found (need pcbnew)"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(spec, f, ensure_ascii=False)
        spec_path = f.name
    try:
        proc = subprocess.run([kp, str(_HELPER), "--input", spec_path],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "helper timed out"}
    finally:
        Path(spec_path).unlink(missing_ok=True)
    # Helper prints JSON to BOTH stdout and stderr (macOS wx eats stdout
    # under subprocess sometimes) — scan both, take the last JSON line.
    combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
    json_lines = [ln for ln in combined.splitlines()
                  if ln.lstrip().startswith("{")]
    if not json_lines:
        return {"ok": False, "error": "no JSON from helper",
                "stderr": (proc.stderr or "")[:400]}
    try:
        return json.loads(json_lines[-1])
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"helper output parse: {e}"}
