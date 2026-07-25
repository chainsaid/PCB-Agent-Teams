#!/usr/bin/env python3
"""draw-pcb toolbox tool: refit_board

Phase D finishing step — shrink the Edge.Cuts outline to hug the actual
placement. The board outline is sized once at init_layout from CLAUDE.md
pack_density; after the agentic loop compacts components, that outline is
stale (too loose). This re-fits the outer rectangle to the real footprint
extent + margin and redraws the isolation slot continuous at its existing x.

Run order in Phase D: refit_board -> bridge_slot -> add_zones -> check_zones -> run_drc
(refit must precede bridge_slot + add_zones — both read Edge.Cuts).

Output JSON includes `fill_ratio` (courtyard area / board area) — the
compactness metric. A very low fill_ratio means the placement is still too
spread out; tighten it in the loop before refitting.

Usage:
  refit_board.py <board.kicad_pcb> [--margin 2.5]

Wraps _kicad_python_helper.py's refit_board mode (needs KiCad's pcbnew).
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _kicad import call_helper  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Re-fit Edge.Cuts to placement")
    ap.add_argument("pcb", help="path to .kicad_pcb")
    ap.add_argument("--margin", type=float, default=2.5,
                    help="board edge margin around footprint extent, mm")
    args = ap.parse_args()

    if not Path(args.pcb).exists():
        print(json.dumps({"ok": False, "error": f"not found: {args.pcb}"}))
        return 1

    result = call_helper({
        "mode": "refit_board",
        "pcb_path": str(args.pcb),
        "output_pcb": str(args.pcb),
        "margin_mm": args.margin,
    })
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
