#!/usr/bin/env python3
"""draw-pcb toolbox tool: route

Phase E — auto-route a placed .kicad_pcb with the vendored KiCadRoutingTools
(KRT): a Rust-accelerated A* autorouter. Run AFTER placement is route-ready
(route-ready 验收 all pass) and Phase D (refit_board / bridge_slot /
add_zones / DRC clean) has finished.

Routes a COPY by default (`<stem>_routed.kicad_pcb`) so the placement-only
board is preserved — placement and routing are separate deliverables and the
user may want to re-place without the routed copper in the way.

KRT's Rust module (grid_router.so) must be built once via
  vendor/KiCadRoutingTools/build_router.py
This tool only runs the router; it does not build.

This tool is NOT a bare `route <pcb>` call: trace widths, which nets are
power nets, controlled impedance, net ordering and via sizing are CIRCUIT
judgments — classify the nets first, then choose the recipe. The judgment
framework is references/routing_strategy.md. Any flag left unset falls back
to KRT's own default, so route only what the circuit needs.

Usage:
  route.py <placed.kicad_pcb> [--output X] [--in-place] [--keep-zones]
           [--board-edge-clearance 0.6] [--nets PAT ...]
           [--track-width MM] [--power-nets NET ... --power-nets-widths MM ...]
           [--ordering {inside_out,mps,original}] [--via-size MM] [--via-drill MM]
           [--clearance MM] [--layers LAYER ...] [--impedance OHM]

Output JSON: {ok, output_pcb, routed_single, multipoint_pads, failed, vias,
recipe, zones_stripped}.

Copper pours are stripped from the routed copy by default (see --keep-zones);
re-pour with add_zones.py afterwards, THEN run DRC.
Run run_drc.py on the output afterwards — KRT reports its own success but
DRC is the geometric final word.
"""
import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

KRT_DIR = (Path(__file__).resolve().parent.parent
           / "vendor" / "KiCadRoutingTools")
KRT_ROUTE = KRT_DIR / "route.py"


def main() -> int:
    ap = argparse.ArgumentParser(description="Auto-route a placed PCB (KRT)")
    ap.add_argument("pcb", help="path to the placed .kicad_pcb")
    ap.add_argument("--output", help="output path (default <stem>_routed.kicad_pcb)")
    ap.add_argument("--in-place", action="store_true",
                    help="route the input file itself (no copy). Requires "
                         "--keep-zones: stripping zones in place would destroy "
                         "the placement board's pour with no copy to fall back on")
    ap.add_argument("--keep-zones", action="store_true",
                    help="do NOT strip copper pours before routing. Default is "
                         "to strip them: KRT reads a zone as proof its net is "
                         "already connected (judged on the outline) and skips "
                         "that net entirely, which leaves unconnected copper "
                         "islands after the post-route re-pour. Re-pour with "
                         "add_zones afterwards either way.")
    ap.add_argument("--board-edge-clearance", type=float, default=0.6,
                    help="trace-to-board-edge clearance mm (default 0.6; "
                         "the create_pcb design rule is 0.5, 0.6 keeps margin)")
    ap.add_argument("--nets", nargs="+",
                    help="net name patterns to route (default: all nets)")
    # --- routing recipe: per references/routing_strategy.md, unset = KRT default ---
    ap.add_argument("--track-width", type=float, metavar="MM",
                    help="base trace width mm for signal nets "
                         "(unset → KRT default ~0.1mm)")
    ap.add_argument("--power-nets", nargs="+", metavar="NET",
                    help="net patterns to route as wide power traces "
                         "(power rails / multi-pad supply nets)")
    ap.add_argument("--power-nets-widths", nargs="+", type=float, metavar="MM",
                    help="trace width mm, paired positionally with --power-nets")
    ap.add_argument("--ordering", choices=["inside_out", "mps", "original"],
                    help="net routing order (unset → KRT default inside_out; "
                         "mps for congested boards, original for re-routes)")
    ap.add_argument("--via-size", type=float, metavar="MM",
                    help="via outer diameter mm (unset → KRT default)")
    ap.add_argument("--via-drill", type=float, metavar="MM",
                    help="via drill diameter mm (unset → KRT default)")
    ap.add_argument("--clearance", type=float, metavar="MM",
                    help="track-to-track clearance mm (unset → KRT default)")
    ap.add_argument("--layers", nargs="+", metavar="LAYER",
                    help="copper layers to route on (unset → KRT default F.Cu B.Cu)")
    ap.add_argument("--impedance", type=float, metavar="OHM",
                    help="controlled-impedance target for diff / ADC nets")
    args = ap.parse_args()

    src = Path(args.pcb)
    if not src.exists():
        print(json.dumps({"ok": False, "error": f"not found: {src}"}))
        return 1
    if not KRT_ROUTE.exists():
        print(json.dumps({"ok": False,
                          "error": f"KRT not vendored at {KRT_DIR}"}))
        return 1
    rust_so = KRT_DIR / "rust_router" / "grid_router.so"
    if not rust_so.exists():
        print(json.dumps({"ok": False, "error":
                          "grid_router.so missing — run "
                          "vendor/KiCadRoutingTools/build_router.py first"}))
        return 1
    if args.power_nets_widths and not args.power_nets:
        print(json.dumps({"ok": False, "error":
                          "--power-nets-widths needs --power-nets"}))
        return 1
    if (args.power_nets and args.power_nets_widths
            and len(args.power_nets) != len(args.power_nets_widths)):
        print(json.dumps({"ok": False, "error":
                          "--power-nets and --power-nets-widths must pair 1:1 "
                          f"({len(args.power_nets)} nets vs "
                          f"{len(args.power_nets_widths)} widths)"}))
        return 1

    # --in-place + zone stripping would delete the placement board's Phase D
    # pour with no copy to fall back on, and iron rule 4 says re-routes restart
    # from that original. Refuse instead of silently destroying it.
    if args.in_place and not args.keep_zones:
        print(json.dumps({"ok": False, "error":
                          "--in-place would strip the placement board's own "
                          "copper pour, irreversibly. Drop --in-place (the "
                          "default writes <stem>_routed.kicad_pcb), or add "
                          "--keep-zones if you really mean to route in place."}))
        return 1

    if args.in_place:
        out = src
    else:
        out = Path(args.output) if args.output else \
            src.with_name(src.stem + "_routed.kicad_pcb")
        shutil.copy2(src, out)

    # Strip copper pours before routing. KRT never treats zone copper as an
    # obstacle, but it DOES treat a zone as proof that its net is already
    # connected (filter_already_routed -> check_connected, judged on the zone
    # OUTLINE, not the actual fill) — so a poured net gets skipped entirely,
    # and the post-route re-pour can then split that copper into islands that
    # nothing connects. An A/B run on one placement showed pour-first leaving
    # unconnected GND islands plus a starved thermal, and strip-first leaving
    # none. Rule areas (keepout / placement) are preserved — they are matched
    # by their own keywords, NOT by "has no net": every zone carries a net
    # field, so a net-based guard would never fire.
    stripped = 0
    if not args.keep_zones:
        text = out.read_text(encoding="utf-8")
        kept, i, truncated = [], 0, False
        while True:
            j = text.find("\n\t(zone\n", i)
            if j < 0:
                kept.append(text[i:])
                break
            kept.append(text[i:j])
            depth, p, in_str, closed = 0, j + 1, False, False
            while p < len(text):
                c = text[p]
                if c == '"' and text[p - 1] != "\\":
                    in_str = not in_str
                elif not in_str:
                    if c == "(":
                        depth += 1
                    elif c == ")":
                        depth -= 1
                        if depth == 0:
                            closed = True
                            break
                p += 1
            if not closed:                      # unbalanced zone block
                truncated = True
                break
            block = text[j:p + 1]
            # Every zone carries a net field, so "has no net" can never be the
            # guard. Rule areas are identified by their own keywords.
            if "(keepout" in block or "(placement" in block:
                kept.append(block)
            else:
                stripped += 1
            i = p + 1
        new_text = "".join(kept)
        if truncated or new_text.count("(") != new_text.count(")"):
            print(json.dumps({"ok": False, "error":
                              "zone strip produced an unbalanced .kicad_pcb — "
                              "aborted without writing. Re-run with "
                              "--keep-zones and re-pour manually.",
                              "output_pcb": str(out)}))
            return 1
        if stripped:
            out.write_text(new_text, encoding="utf-8")

    cmd = [sys.executable, str(KRT_ROUTE), str(out), "--overwrite",
           "--board-edge-clearance", str(args.board_edge_clearance)]
    if args.nets:
        cmd += ["--nets"] + args.nets
    if args.track_width is not None:
        cmd += ["--track-width", str(args.track_width)]
    if args.power_nets:
        cmd += ["--power-nets"] + args.power_nets
    if args.power_nets_widths:
        cmd += ["--power-nets-widths"] + [str(w) for w in args.power_nets_widths]
    if args.ordering:
        cmd += ["--ordering", args.ordering]
    if args.via_size is not None:
        cmd += ["--via-size", str(args.via_size)]
    if args.via_drill is not None:
        cmd += ["--via-drill", str(args.via_drill)]
    if args.clearance is not None:
        cmd += ["--clearance", str(args.clearance)]
    if args.layers:
        cmd += ["--layers"] + args.layers
    if args.impedance is not None:
        cmd += ["--impedance", str(args.impedance)]

    # Echo the non-default recipe back so the caller can print what was chosen.
    recipe = {k: v for k, v in {
        "track_width": args.track_width,
        "power_nets": args.power_nets,
        "power_nets_widths": args.power_nets_widths,
        "ordering": args.ordering,
        "via_size": args.via_size,
        "via_drill": args.via_drill,
        "clearance": args.clearance,
        "layers": args.layers,
        "impedance": args.impedance,
    }.items() if v is not None}

    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    summary = None
    for ln in (proc.stdout or "").splitlines():
        if ln.startswith("JSON_SUMMARY:"):
            try:
                summary = json.loads(ln[len("JSON_SUMMARY:"):].strip())
            except json.JSONDecodeError:
                pass
    if summary is None:
        print(json.dumps({"ok": False, "error": "KRT produced no summary",
                          "stderr": (proc.stderr or "")[:600],
                          "stdout_tail": (proc.stdout or "")[-600:]}))
        return 1

    failed = (summary.get("failed", 0)
              + len(summary.get("failed_multipoint", [])))
    print(json.dumps({
        "ok": failed == 0,
        "output_pcb": str(out),
        "routed_single": summary.get("successful", 0),
        "multipoint_pads": f"{summary.get('multipoint_pads_connected', 0)}/"
                           f"{summary.get('multipoint_pads_total', 0)}",
        "failed": failed,
        "vias": summary.get("total_vias", 0),
        "recipe": recipe or "all KRT defaults",
        "zones_stripped": stripped,
        "next": ("re-pour with add_zones.py (replay every net/layer Phase D "
                 "used), THEN run_drc.py — DRC on stale copper is all false "
                 "clearance violations"),
    }, ensure_ascii=False, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
