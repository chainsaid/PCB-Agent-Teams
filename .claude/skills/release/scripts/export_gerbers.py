#!/usr/bin/env python3
"""Export KiCad fabrication outputs with kicad-cli.

Outputs:
  - Gerber layers
  - Excellon drill files + map/report
  - CSV position file (CPL / pick-and-place)
  - Optional assembly BOM from the matching schematic
  - ZIP archive containing the generated fab files
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

# Tried newest-first — matches the vendored KiCadRoutingTools/install_plugin.py
# precedent so a KiCad 9.x install isn't left unsupported by an assumed-10.0 path.
_KICAD_WINDOWS_VERSIONS = ["10.0", "9.99", "9.0"]


def _windows_kicad_cli_candidates() -> list[str]:
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


KICAD_CLI_CANDIDATES = [
    "/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli",
    "/usr/bin/kicad-cli",
    "/usr/local/bin/kicad-cli",
    "/snap/kicad/current/bin/kicad-cli",
] + _windows_kicad_cli_candidates()

DEFAULT_NON_COPPER_LAYERS = [
    "F.Paste",
    "B.Paste",
    "F.Silkscreen",
    "B.Silkscreen",
    "F.Mask",
    "B.Mask",
    "Edge.Cuts",
]


def find_kicad_cli(explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.exists():
            return str(path)
        raise FileNotFoundError(f"kicad-cli not found: {explicit}")

    for candidate in KICAD_CLI_CANDIDATES:
        if Path(candidate).exists():
            return candidate

    found = shutil.which("kicad-cli")
    if found:
        return found

    raise FileNotFoundError("kicad-cli not found")


def parse_board_layers(pcb_path: Path) -> list[str]:
    """Return copper layer names from the board file, preserving stack order."""
    text = pcb_path.read_text(encoding="utf-8", errors="ignore")
    layers_match = re.search(r"\(layers\s*(.*?)\n\s*\)", text, re.S)
    if not layers_match:
        return ["F.Cu", "B.Cu"]

    layers: list[str] = []
    for line in layers_match.group(1).splitlines():
        match = re.search(r'\(\s*\d+\s+"([^"]+)"\s+([^)]+)\)', line)
        if not match:
            continue
        name = match.group(1)
        layer_type = match.group(2)
        if name.endswith(".Cu") and any(token in layer_type for token in ("signal", "power")):
            layers.append(name)

    if "F.Cu" not in layers:
        layers.insert(0, "F.Cu")
    if "B.Cu" not in layers:
        layers.append("B.Cu")
    return layers


def find_matching_schematic(pcb_path: Path, explicit: str | None = None) -> Path | None:
    if explicit:
        sch = Path(explicit).expanduser().resolve()
        if not sch.exists():
            raise FileNotFoundError(f"schematic not found: {sch}")
        return sch

    same_stem = pcb_path.with_suffix(".kicad_sch")
    if same_stem.exists():
        return same_stem

    candidates = sorted(pcb_path.parent.glob("*.kicad_sch"))
    if len(candidates) == 1:
        return candidates[0]
    return None


def run(cmd: list[str]) -> dict:
    proc = subprocess.run(cmd, text=True, capture_output=True)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout.strip(),
        "stderr": proc.stderr.strip(),
    }


def require_ok(result: dict) -> None:
    if result["returncode"] != 0:
        cmd = " ".join(result["cmd"])
        raise RuntimeError(
            f"command failed ({result['returncode']}): {cmd}\n"
            f"stdout:\n{result['stdout']}\n\nstderr:\n{result['stderr']}"
        )


def make_zip(src_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src_dir.rglob("*")):
            if path == zip_path or path.is_dir():
                continue
            zf.write(path, path.relative_to(src_dir))


def export_fab(args: argparse.Namespace) -> dict:
    pcb_path = Path(args.pcb).expanduser().resolve()
    if not pcb_path.exists():
        raise FileNotFoundError(f"PCB file not found: {pcb_path}")
    if pcb_path.suffix != ".kicad_pcb":
        raise ValueError(f"expected .kicad_pcb, got: {pcb_path}")

    kicad_cli = find_kicad_cli(args.kicad_cli)
    schematic = find_matching_schematic(pcb_path, args.schematic)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    base_output = Path(args.output).expanduser().resolve() if args.output else pcb_path.parent / "fab" / f"release_{pcb_path.stem}_{timestamp}"
    gerber_dir = base_output / "gerbers"
    assembly_dir = base_output / "assembly"
    gerber_dir.mkdir(parents=True, exist_ok=True)
    assembly_dir.mkdir(parents=True, exist_ok=True)

    layers = args.layers.split(",") if args.layers else parse_board_layers(pcb_path) + DEFAULT_NON_COPPER_LAYERS
    layers_arg = ",".join(dict.fromkeys(layer.strip() for layer in layers if layer.strip()))

    results: dict[str, dict | None] = {}

    gerbers_cmd = [
        kicad_cli,
        "pcb",
        "export",
        "gerbers",
        "--output",
        str(gerber_dir),
    ]
    if args.board_plot_params:
        gerbers_cmd.append("--board-plot-params")
    else:
        gerbers_cmd.extend(["--layers", layers_arg])
    gerbers_cmd.extend([
        "--subtract-soldermask",
        "--check-zones",
        str(pcb_path),
    ])
    results["gerbers"] = run(gerbers_cmd)
    require_ok(results["gerbers"])

    drill_report = gerber_dir / f"{pcb_path.stem}-drill_report.txt"
    drill_cmd = [
        kicad_cli,
        "pcb",
        "export",
        "drill",
        "--output",
        str(gerber_dir),
        "--format",
        "excellon",
        "--excellon-units",
        "mm",
        "--generate-map",
        "--map-format",
        "gerberx2",
        "--generate-report",
        "--report-path",
        str(drill_report),
        str(pcb_path),
    ]
    results["drill"] = run(drill_cmd)
    require_ok(results["drill"])

    pos_path = assembly_dir / f"{pcb_path.stem}_positions.csv"
    pos_cmd = [
        kicad_cli,
        "pcb",
        "export",
        "pos",
        "--output",
        str(pos_path),
        "--side",
        "both",
        "--format",
        "csv",
        "--units",
        "mm",
        "--exclude-dnp",
        str(pcb_path),
    ]
    if args.smd_only:
        pos_cmd.insert(-1, "--smd-only")
    results["positions"] = run(pos_cmd)
    require_ok(results["positions"])

    bom_path = None
    if schematic and not args.no_bom:
        bom_path = assembly_dir / f"{pcb_path.stem}_assembly_bom.csv"
        bom_cmd = [
            kicad_cli,
            "sch",
            "export",
            "bom",
            "--output",
            str(bom_path),
            "--fields",
            "Reference,Value,Footprint,QUANTITY,DNP,MPN,LCSC Part #",
            "--labels",
            "Refs,Value,Footprint,Qty,DNP,MPN,LCSC Part #",
            "--group-by",
            "Value,Footprint,MPN,LCSC Part #",
            "--exclude-dnp",
            str(schematic),
        ]
        results["bom"] = run(bom_cmd)
        require_ok(results["bom"])
    else:
        results["bom"] = None

    manifest = {
        "ok": True,
        "pcb": str(pcb_path),
        "schematic": str(schematic) if schematic else None,
        "kicad_cli": kicad_cli,
        "output_dir": str(base_output),
        "gerber_dir": str(gerber_dir),
        "assembly_dir": str(assembly_dir),
        "layers": layers_arg.split(","),
        "positions_csv": str(pos_path),
        "assembly_bom_csv": str(bom_path) if bom_path else None,
        "commands": results,
    }
    zip_path = base_output / f"{pcb_path.stem}_fab.zip"
    manifest["zip"] = str(zip_path)
    manifest_path = base_output / "fab_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    make_zip(base_output, zip_path)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export KiCad Gerber/Drill/CPL/BOM fab files.")
    parser.add_argument("pcb", help="Path to .kicad_pcb")
    parser.add_argument("-o", "--output", help="Output release directory. Default: <pcb_dir>/fab/release_<stem>_<timestamp>")
    parser.add_argument("--schematic", help="Optional matching .kicad_sch for assembly BOM export")
    parser.add_argument("--layers", help="Comma-separated Gerber layers. Default: all copper + paste/silk/mask/Edge.Cuts")
    parser.add_argument("--kicad-cli", help="Explicit kicad-cli path")
    parser.add_argument("--board-plot-params", action="store_true", help="Use plot settings stored in the board file")
    parser.add_argument("--smd-only", action="store_true", default=False, help="Opt-in: export only SMD footprints (use only when fab does SMT-only and refuses THT lines)")
    parser.add_argument("--all-pos", action="store_true", help="Deprecated no-op; CPL now includes THT by default. Kept for backward compat.")
    parser.add_argument("--no-bom", action="store_true", help="Skip schematic BOM export")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        manifest = export_fab(args)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
