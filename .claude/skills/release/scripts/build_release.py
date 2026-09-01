"""release skill main orchestrator.

Phase 5 entry point. Aggregates upstream artifacts into Projects/<name>/release/<ts>/
+ release_<ts>.zip + ORDER_GUIDE.md.

Gate: requires bom-readiness sentinel valid (all_pass=true) and .kicad_pcb mtime
not newer than sentinel verified_at. (When check-pcb verdict.json protocol lands,
the gate will switch to that — see spec §10.)
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts import coverage_scan, distributor_csv, load_preferences, render_order_guide  # type: ignore
else:
    from . import coverage_scan, distributor_csv, load_preferences, render_order_guide

WORKSPACE_ROOT = Path(__file__).resolve().parents[4]
KICAD_EXPORT_GERBERS = WORKSPACE_ROOT / ".claude/skills/release/scripts/export_gerbers.py"
VENV_PYTHON = WORKSPACE_ROOT / (".venv/Scripts/python.exe" if sys.platform == "win32" else ".venv/bin/python")

# JST = UTC+9. Use a fixed offset rather than zoneinfo to avoid platform tz-data issues.
from datetime import timedelta
JST = timezone(timedelta(hours=9), name="JST")


# ---------- pcb / sch resolution ----------

def _find_pcb(project_dir: Path) -> Path | None:
    """Resolve the canonical .kicad_pcb for a project.

    Supports two layouts:
      flat:    Projects/<name>/kicad/<name>.kicad_pcb
      nested:  Projects/<name>/kicad/<name>/<name>.kicad_pcb (circuit-synth default)

    Picks the file whose stem matches the project name to avoid backup variants
    like `<name>_ZZ.kicad_pcb` or `<name>.kicad_pcb.pre_fix`.

    draw-pcb's Phase E router deliberately writes routed output to a sibling
    `<name>_routed.kicad_pcb` rather than overwriting the placement-only board
    (so a user can re-place without the routed copper in the way). When that
    routed copy exists it's usually the actual board to fab — *unless* the
    user went back into KiCad GUI and hand-edited the unrouted placement-only
    board afterward (an explicitly supported workflow per draw-pcb's own
    docs), which makes the routed copy stale. Compare mtimes rather than
    always preferring `_routed`, so an edit made after routing doesn't get
    silently ignored in favor of copper that no longer matches the current
    placement.
    """
    name = project_dir.name
    routed_candidates = [
        project_dir / "kicad" / f"{name}_routed.kicad_pcb",
        project_dir / "kicad" / name / f"{name}_routed.kicad_pcb",
    ]
    plain_candidates = [
        project_dir / "kicad" / f"{name}.kicad_pcb",
        project_dir / "kicad" / name / f"{name}.kicad_pcb",
    ]
    routed = next((c for c in routed_candidates if c.is_file()), None)
    plain = next((c for c in plain_candidates if c.is_file()), None)
    if routed and plain:
        return routed if routed.stat().st_mtime >= plain.stat().st_mtime else plain
    if routed or plain:
        return routed or plain
    # Fallback: any .kicad_pcb under kicad/, prefer ones whose stem matches project name.
    matches = [p for p in (project_dir / "kicad").rglob("*.kicad_pcb") if p.is_file()]
    name_match = [p for p in matches if p.stem == name]
    if name_match:
        return name_match[0]
    return matches[0] if matches else None


def _find_sch(project_dir: Path) -> Path | None:
    name = project_dir.name
    candidates = [
        project_dir / "kicad" / f"{name}.kicad_sch",
        project_dir / "kicad" / name / f"{name}.kicad_sch",
    ]
    for c in candidates:
        if c.is_file():
            return c
    matches = [p for p in (project_dir / "kicad").rglob("*.kicad_sch") if p.is_file()]
    name_match = [p for p in matches if p.stem == name]
    if name_match:
        return name_match[0]
    return matches[0] if matches else None


# ---------- gate ----------

def _check_gate(project_dir: Path) -> tuple[bool, str, dict]:
    sentinel_path = project_dir / "datasheets" / ".bom_readiness.json"
    if not sentinel_path.exists():
        return False, f"bom-readiness sentinel missing: {sentinel_path}", {}
    try:
        sentinel = json.loads(sentinel_path.read_text())
    except json.JSONDecodeError as e:
        return False, f"sentinel not valid JSON: {e}", {}
    if not sentinel.get("all_pass"):
        return False, "sentinel.all_pass=false — re-run bom-readiness", sentinel

    pcb = _find_pcb(project_dir)
    if pcb is None:
        return False, "no .kicad_pcb in kicad/ (looked at kicad/*.kicad_pcb and kicad/<name>/<name>.kicad_pcb)", sentinel

    verified_at = sentinel.get("verified_at")
    if verified_at:
        try:
            sentinel_ts = datetime.fromisoformat(verified_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            sentinel_ts = 0.0
        pcb_ts = pcb.stat().st_mtime
        if pcb_ts > sentinel_ts + 1.0:
            return False, (
                f"PCB modified ({datetime.fromtimestamp(pcb_ts).isoformat()}) "
                f"after BOM check ({verified_at}) — re-run bom-readiness to refresh "
                f"the sentinel (and re-run check-pcb as needed); the gate keys off "
                f".bom_readiness.json verified_at, which check-pcb does not touch"
            ), sentinel

    return True, "OK", sentinel


# ---------- board summary (best-effort) ----------

GET_GEOMETRY = WORKSPACE_ROOT / ".claude/skills/draw-pcb/scripts/tools/get_geometry.py"


def _board_summary(pcb_path: Path) -> dict:
    """Best-effort board stats for ORDER_GUIDE.

    SMD/THT counts and board size come from get_geometry.py (reads the real
    per-footprint `type` and Edge.Cuts bbox via the pcbnew API) rather than
    substring-counting `"(footprint "` in the raw text, which can only ever
    produce a total count — it has no way to tell SMD from THT, so a prior
    version of this function hardcoded n_tht to "0" unconditionally, silently
    misreporting every board that has any through-hole part (connectors,
    electrolytic caps, THT headers) as 100% SMD.
    """
    fallback = {
        "thickness_mm": "?", "layers": "?", "size_mm": "?",
        "n_components": "?", "n_smt": "?", "n_tht": "?",
    }
    summary = dict(fallback)

    try:
        text = pcb_path.read_text(errors="replace")
    except OSError:
        text = ""
    if text:
        m = re.search(r"\(general\b.*?\(thickness\s+([\d.]+)\)", text, re.DOTALL)
        if m:
            summary["thickness_mm"] = m.group(1)
        layers_block = re.search(r"\(layers\b(.*?)\n\t\)", text, re.DOTALL)
        if layers_block:
            summary["layers"] = str(len(re.findall(r'"\s*signal\)', layers_block.group(1))))

    try:
        proc = subprocess.run(
            [str(VENV_PYTHON), str(GET_GEOMETRY), str(pcb_path), "--no-pads"],
            capture_output=True, text=True, timeout=60,
        )
        geom = json.loads(proc.stdout)
        if geom.get("ok"):
            footprints = geom.get("footprints", [])
            summary["n_components"] = str(len(footprints))
            summary["n_smt"] = str(sum(1 for f in footprints if f.get("type") == "smd"))
            summary["n_tht"] = str(sum(1 for f in footprints if f.get("type") == "through_hole"))
            board = geom.get("board") or {}
            if "w" in board and "h" in board:
                summary["size_mm"] = f"{board['w']:.2f} x {board['h']:.2f}"
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError, ValueError):
        pass  # keep whatever the regex pass above already filled in, rest stays "?"

    return summary


# ---------- step runners ----------

def _step_export_gerbers(project_dir: Path, release_pcb_dir: Path, skip: bool) -> None:
    if skip:
        release_pcb_dir.mkdir(parents=True, exist_ok=True)
        (release_pcb_dir / ".SKIPPED").write_text(
            "export_gerbers skipped via --skip-fab-export\n"
        )
        return
    pcb = _find_pcb(project_dir)
    sch = _find_sch(project_dir)
    cmd = [
        str(VENV_PYTHON), str(KICAD_EXPORT_GERBERS), str(pcb),
        "--output", str(release_pcb_dir),
    ]
    if sch is not None:
        cmd.extend(["--schematic", str(sch)])
    subprocess.run(cmd, check=True)


# ---------- reuse mode ----------

def _find_release_dir(project_dir: Path, release_id: str) -> Path | None:
    """Resolve release_id (with or without 'rel_' prefix) to release/<ts>/."""
    rel_root = project_dir / "release"
    if not rel_root.is_dir():
        return None
    candidates = [release_id]
    if release_id.startswith("rel_"):
        candidates.append(release_id[4:])
    else:
        candidates.append(f"rel_{release_id}")
    for cand in candidates:
        d = rel_root / cand
        if d.is_dir():
            return d
    # Fallback: substring match (most recent first)
    for d in sorted(rel_root.iterdir(), reverse=True):
        if d.is_dir() and (release_id in d.name or release_id.removeprefix("rel_") in d.name):
            return d
    return None


def _replay_kicad_commands(manifest_path: Path) -> list[dict]:
    """Replay kicad-cli commands recorded in fab_manifest.json#commands.

    Reproduces the exact flag set used the first time (e.g. --subtract-soldermask,
    --check-zones), avoiding the silent drift that happens when LLM hand-writes
    kicad-cli on a re-run and forgets a flag.

    Returns per-step result dicts: {step, returncode, ok}.
    """
    manifest = json.loads(manifest_path.read_text())
    commands = manifest.get("commands") or {}
    if not commands:
        raise RuntimeError(f"manifest has no commands section: {manifest_path}")
    results: list[dict] = []
    for step_name, step in commands.items():
        cmd = step.get("cmd")
        if not cmd:
            results.append({"step": step_name, "ok": False, "returncode": None,
                           "error": "no cmd recorded"})
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        results.append({
            "step": step_name,
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout_tail": proc.stdout[-200:] if proc.stdout else "",
            "stderr_tail": proc.stderr[-200:] if proc.stderr else "",
        })
    return results


def _run_reuse(project_dir: Path, release_id: str) -> int:
    """In-place revision: replay the original kicad-cli commands from manifest.

    Use case: BOM micro-tweak (swap a few caps, change a value) where the .kicad_pcb
    has been edited but the release/<ts>/ scaffold + ORDER_GUIDE + procurement CSVs
    are still valid. Re-derives just the fab artifacts (Gerber / drill / pos / CPL)
    without re-doing coverage scan / ORDER_GUIDE / zip.
    """
    rel_dir = _find_release_dir(project_dir, release_id)
    if rel_dir is None:
        print(f"ERROR: release dir not found for id '{release_id}' under "
              f"{project_dir / 'release'}", file=sys.stderr)
        return 1
    manifest_path = rel_dir / "pcb_fab" / "fab_manifest.json"
    if not manifest_path.exists():
        print(f"ERROR: fab_manifest.json missing: {manifest_path}\n"
              f"       --reuse requires a prior full release run that wrote this file.",
              file=sys.stderr)
        return 1

    print(f"reuse: replaying {manifest_path}")
    results = _replay_kicad_commands(manifest_path)
    n_ok = sum(1 for r in results if r["ok"])
    n_fail = len(results) - n_ok
    for r in results:
        marker = "✅" if r["ok"] else "❌"
        print(f"  {marker} {r['step']:<10} rc={r['returncode']}")
        if not r["ok"] and r.get("stderr_tail"):
            print(f"     stderr: {r['stderr_tail']}")

    # Re-pack the fab zip so distributors get the refreshed Gerber set.
    #
    # Name it from the manifest's own recorded zip path, not from bare
    # project_name: export_gerbers.py names every artifact (including the
    # zip) after the *pcb file's* stem, and since _find_pcb can resolve to a
    # sibling like "<name>_routed.kicad_pcb" (draw-pcb Phase E), the original
    # full build's zip was "<name>_routed_fab.zip" — reconstructing from
    # project_name here would silently produce and leave stale a
    # differently-named zip while ORDER_GUIDE.md still points at the
    # original.
    pcb_fab_dir = rel_dir / "pcb_fab"
    manifest = json.loads(manifest_path.read_text())
    recorded_zip = manifest.get("zip")
    zip_path = pcb_fab_dir / Path(recorded_zip).name if recorded_zip else \
        pcb_fab_dir / f"{Path(manifest.get('pcb', project_dir.name)).stem}_fab.zip"
    if pcb_fab_dir.is_dir():
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in pcb_fab_dir.rglob("*"):
                if f.is_file() and f != zip_path and "_archive" not in f.parts:
                    zf.write(f, f.relative_to(pcb_fab_dir))
        print(f"  📦 fab zip refreshed: {zip_path}")

    print(f"\nreuse summary: {n_ok}/{len(results)} OK, {n_fail} fail")
    print(f"⚠ NOT regenerated (intentional): release_manifest.json, ORDER_GUIDE.md, "
          f"distributor CSVs, coverage scan. If BOM changed, do a fresh full release.")
    return 0 if n_fail == 0 else 1


def _step_copy_procurement(project_dir: Path, release_proc_dir: Path, project_name: str) -> Path:
    src = project_dir / "datasheets" / f"bom_{project_name}.csv"
    if not src.exists():
        raise FileNotFoundError(
            f"采购 BOM not found: {src} — run bom-readiness first"
        )
    release_proc_dir.mkdir(parents=True, exist_ok=True)
    dst = release_proc_dir / src.name
    shutil.copy2(src, dst)
    return dst


def _step_copy_datasheets(project_dir: Path, release_ds_dir: Path) -> None:
    src = project_dir / "datasheets"
    release_ds_dir.mkdir(parents=True, exist_ok=True)
    for pdf in src.glob("*.pdf"):
        shutil.copy2(pdf, release_ds_dir / pdf.name)


def _step_copy_docs(project_dir: Path, release_docs_dir: Path) -> None:
    release_docs_dir.mkdir(parents=True, exist_ok=True)
    src = project_dir / "reports"
    if src.exists():
        for pdf in src.glob("*.pdf"):
            shutil.copy2(pdf, release_docs_dir / pdf.name)
    if not any(release_docs_dir.iterdir()):
        (release_docs_dir / ".gitkeep").write_text("")


def _step_zip_release(release_dir: Path, release_id: str) -> Path:
    zip_path = release_dir / f"release_{release_id}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file in release_dir.rglob("*"):
            if file.is_file() and file != zip_path:
                zf.write(file, file.relative_to(release_dir))
    return zip_path


def _write_manifest(
    release_dir: Path, project_name: str, release_id: str,
    sentinel: dict, coverage: dict,
) -> Path:
    manifest = {
        "release_id": release_id,
        "project": project_name,
        "generated_at_jst": datetime.now(JST).isoformat(),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "upstream": {
            "bom_readiness_verified_at": sentinel.get("verified_at"),
            "bom_readiness_summary": sentinel.get("summary"),
        },
        "coverage": {
            "n_unique_mpn": coverage["n_unique_mpn"],
            "single_vendor_coverage": coverage["single_vendor_coverage"],
            "recommended_paths": coverage["recommended_paths"],
        },
    }
    path = release_dir / "release_manifest.json"
    path.write_text(json.dumps(manifest, indent=2))
    return path


# ---------- main ----------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Phase 5 release packager")
    p.add_argument("project_dir", help="Path to Projects/<name>/")
    p.add_argument("--force", action="store_true", help="Overwrite existing release/<ts>/")
    p.add_argument("--skip-fab-export", action="store_true",
                   help="Skip kicad export_gerbers.py call (faster re-runs)")
    p.add_argument("--dry-run", action="store_true", help="Run gate only; do not write files")
    p.add_argument("--reuse", metavar="RELEASE_ID",
                   help="In-place revision: re-run the kicad-cli commands recorded "
                        "in release/<id>/pcb_fab/fab_manifest.json (refreshes Gerber/"
                        "drill/pos/CPL with the exact original flag set, then re-zips "
                        "pcb_fab). Use after a small .kicad_pcb edit when ORDER_GUIDE / "
                        "procurement CSVs / coverage scan don't need to change.")
    args = p.parse_args(argv)

    project_dir = Path(args.project_dir).resolve()
    if not project_dir.is_dir():
        print(f"ERROR: project_dir does not exist: {project_dir}", file=sys.stderr)
        return 1
    project_name = project_dir.name

    if args.reuse:
        return _run_reuse(project_dir, args.reuse)

    ok, msg, sentinel = _check_gate(project_dir)
    print(f"gate: {'PASS' if ok else 'FAIL'} — {msg}")
    if not ok:
        return 1
    if args.dry_run:
        print("dry-run: gate OK, no files written")
        return 0

    now_jst = datetime.now(JST)
    ts = now_jst.strftime("%Y%m%d_%H%M%S")
    release_id = f"rel_{ts}"
    release_dir = project_dir / "release" / ts
    if release_dir.exists() and not args.force:
        print(f"ERROR: {release_dir} already exists. Use --force.", file=sys.stderr)
        return 1
    release_dir.mkdir(parents=True, exist_ok=True)

    pcb = _find_pcb(project_dir)
    if pcb is None:
        print("ERROR: .kicad_pcb disappeared between gate check and run start", file=sys.stderr)
        return 1

    _step_export_gerbers(project_dir, release_dir / "pcb_fab", args.skip_fab_export)

    _step_copy_procurement(project_dir, release_dir / "procurement", project_name)
    distributor_csv.write_distributor_csvs(
        release_dir / "procurement" / f"bom_{project_name}.csv",
        release_dir / "procurement",
    )

    coverage = coverage_scan.scan_coverage(
        project_dir / "datasheets" / "component_selecting"
    )
    board = _board_summary(pcb)

    # Phase 2 4 轴偏好（component-selecting-JP 写）。release SKILL.md 在调本脚本前
    # 已确保该文件存在；这里只读，缺则 fail-fast 让 LLM 重新走 SKILL.md 顶部的
    # 偏好流程（AskUser 4 轴 + record_preferences.py 回写）。
    user_prefs = load_preferences.load_preferences(project_dir)
    if user_prefs is None:
        sys.exit(json.dumps({
            "ok": False,
            "step": "user_preferences",
            "reason": (
                "Projects/{}/_artifacts/component_selecting/user_preferences.json "
                "缺失或 schema 不合法。release skill SKILL.md 顶部的"
                "「Phase 0: 4 轴偏好」步骤未跑——回去 AskUserQuestion 4 轴后调"
                ".claude/skills/component-selecting-JP/scripts/record_preferences.py "
                "回写，再重跑 build_release.py。"
            ).format(project_name),
        }, ensure_ascii=False, indent=2))

    _step_copy_datasheets(project_dir, release_dir / "datasheets")
    _step_copy_docs(project_dir, release_dir / "docs")

    context = {
        "project": project_name,
        # export_gerbers.py names every fab artifact after the *pcb file's*
        # stem, not the bare project name — the two only coincide when there's
        # no sibling variant (e.g. draw-pcb's Phase E "<name>_routed.kicad_pcb").
        # The template must reference filenames via this, not `project`.
        "pcb_stem": pcb.stem,
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M JST"),
        "release_id": release_id,
        "board": board,
        "coverage": coverage,
        "user_intent": {
            "channel": user_prefs["channel"],
            "brand": user_prefs["brand"],
            "price_vs_stock": user_prefs["price_vs_stock"],
            "blacklist_mpns": user_prefs["blacklist_mpns"],
            "recommended_path": load_preferences.channel_to_recommended_path(
                user_prefs["channel"]
            ),
            "asked_at": user_prefs.get("asked_at"),
        },
        "gate": {
            "status": "PASS",
            # This is bom-readiness's sentinel timestamp, not check-pcb's —
            # check-pcb has no machine-readable verdict file of its own yet
            # (see release_internals.md: "读 check-pcb verdict.json（gate 协议，
            # 待落地）"), so there is nothing else to report here. Label it for
            # what it actually is rather than let the template call it a
            # "check-pcb gate" timestamp it never was.
            "timestamp": sentinel.get("verified_at", "unknown"),
        },
    }
    render_order_guide.render_all(context, release_dir)

    _write_manifest(release_dir, project_name, release_id, sentinel, coverage)

    zip_path = _step_zip_release(release_dir, release_id)

    print(f"\n✅ release built: {release_dir}")
    print(f"   zip: {zip_path}")
    print(f"   user channel preference: {context['user_intent']['channel']}")
    print(f"   coverage recommended paths (algorithmic): "
          f"{coverage['recommended_paths'] or '(none — manual mix)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
