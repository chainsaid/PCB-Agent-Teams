"""End-to-end test for build_release.py using a synthesized fixture project."""

import json
import os
import time
import zipfile
from pathlib import Path

from scripts import build_release


def _make_fixture_project(tmp_path: Path) -> Path:
    proj = tmp_path / "fixture_proj"
    (proj / "kicad").mkdir(parents=True)
    (proj / "datasheets" / "component_selecting").mkdir(parents=True)
    (proj / "reports").mkdir(parents=True)

    (proj / "kicad" / "fixture_proj.kicad_pcb").write_text("(kicad_pcb (version 20221018))")
    (proj / "kicad" / "fixture_proj.py").write_text("# circuit-synth source")

    py = proj / "kicad" / "fixture_proj.py"
    sentinel = {
        "verified_at": "2099-01-01T00:00:00Z",
        "py_file": str(py),
        "py_mtime": py.stat().st_mtime,
        "all_pass": True,
        "components": [],
        "summary": {"total": 1, "pass": 1, "fail": 0},
    }
    (proj / "datasheets" / ".bom_readiness.json").write_text(json.dumps(sentinel))

    bom = "Qty,Refs,MPN,Category,Footprint,Vendor_Status,Vendor_Stock,Vendor_Url,Datasheet\n"
    bom += "1,U1,AMC1311BDWVR,ic,SOIC-8,active,12756,https://example.com,foo.pdf\n"
    (proj / "datasheets" / "bom_fixture_proj.csv").write_text(bom)

    evidence = {
        "schema_version": "phase2.5_v1",
        "mpn": "AMC1311BDWVR",
        "designators": "U1",
        "qty_per_board": 1,
        "vendor": {
            "primary": "digikey_jp", "active": True,
            "stock": 12756, "price_jpy": 823.2,
            "product_url": "https://www.digikey.com/.../AMC1311BDWVR",
        },
    }
    (proj / "datasheets" / "component_selecting" / "AMC1311BDWVR.json").write_text(
        json.dumps(evidence)
    )

    (proj / "datasheets" / "AMC1311BDWVR.pdf").write_bytes(b"%PDF-1.4 fake")

    # Phase 2 4-axis user preferences (release reads this for ORDER_GUIDE recommendation).
    prefs_dir = proj / "_artifacts" / "component_selecting"
    prefs_dir.mkdir(parents=True, exist_ok=True)
    (prefs_dir / "user_preferences.json").write_text(json.dumps({
        "schema_version": "v1",
        "asked_at": "2026-05-08T01:00:00+00:00",
        "channel": "auto_cheapest",
        "brand": "any",
        "price_vs_stock": "balanced",
        "blacklist_mpns": [],
    }))

    return proj


def test_build_release_dry_run_passes_gate(tmp_path):
    proj = _make_fixture_project(tmp_path)
    rc = build_release.main([str(proj), "--dry-run", "--skip-fab-export"])
    assert rc == 0


def test_build_release_fails_when_user_preferences_missing(tmp_path):
    """release 读不到 user_preferences.json 必须 fail-fast，让 LLM 回头补。"""
    proj = _make_fixture_project(tmp_path)
    (proj / "_artifacts" / "component_selecting" / "user_preferences.json").unlink()

    try:
        build_release.main([str(proj), "--skip-fab-export"])
    except SystemExit as e:
        # SystemExit may carry int rc or a JSON message string
        msg = str(e.code) if e.code is not None else ""
        assert "user_preferences" in msg
        return
    raise AssertionError("expected build_release to SystemExit on missing prefs")


def test_build_release_e2e_writes_full_tree(tmp_path):
    proj = _make_fixture_project(tmp_path)
    rc = build_release.main([str(proj), "--skip-fab-export"])
    assert rc == 0

    releases = list((proj / "release").iterdir())
    assert len(releases) == 1
    rel = releases[0]

    assert (rel / "ORDER_GUIDE.md").exists()
    assert (rel / "coverage_matrix.md").exists()
    assert (rel / "fab_options.md").exists()
    assert (rel / "release_manifest.json").exists()
    assert (rel / "procurement" / "bom_fixture_proj.csv").exists()
    assert (rel / "procurement" / "digikey_bulk.csv").exists()
    assert (rel / "procurement" / "mouser_bom.csv").exists()
    assert (rel / "procurement" / "lcsc_bom.csv").exists()
    assert (rel / "datasheets" / "AMC1311BDWVR.pdf").exists()

    zips = list(rel.glob("release_*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
        assert any(n.endswith("ORDER_GUIDE.md") for n in names)
        assert any(n.endswith("procurement/digikey_bulk.csv") for n in names)


def test_find_pcb_prefers_newer_of_routed_and_plain(tmp_path):
    """draw-pcb's Phase E router writes a sibling `<name>_routed.kicad_pcb`
    rather than overwriting the placement-only board. If the user later goes
    back into KiCad GUI and hand-edits the *unrouted* board (an explicitly
    supported workflow), that edit must win — release should not silently
    fab stale copper just because a `_routed` file happens to exist.
    """
    proj = tmp_path / "proj"
    kicad_dir = proj / "kicad"
    kicad_dir.mkdir(parents=True)
    plain = kicad_dir / "proj.kicad_pcb"
    routed = kicad_dir / "proj_routed.kicad_pcb"

    # Case 1: only routed exists -> routed wins.
    routed.write_text("(kicad_pcb (routed))")
    assert build_release._find_pcb(proj) == routed

    # Case 2: plain edited after routing (hand-edit in GUI) -> plain wins.
    plain.write_text("(kicad_pcb (hand-edited))")
    os.utime(plain, (time.time() + 10, time.time() + 10))
    assert build_release._find_pcb(proj) == plain

    # Case 3: routed re-generated after that edit -> routed wins again.
    os.utime(routed, (time.time() + 20, time.time() + 20))
    assert build_release._find_pcb(proj) == routed


def test_build_release_fails_gate_when_sentinel_missing(tmp_path):
    proj = _make_fixture_project(tmp_path)
    (proj / "datasheets" / ".bom_readiness.json").unlink()
    rc = build_release.main([str(proj), "--skip-fab-export"])
    assert rc == 1


def test_build_release_fails_gate_when_all_pass_false(tmp_path):
    proj = _make_fixture_project(tmp_path)
    sentinel_path = proj / "datasheets" / ".bom_readiness.json"
    s = json.loads(sentinel_path.read_text())
    s["all_pass"] = False
    sentinel_path.write_text(json.dumps(s))
    rc = build_release.main([str(proj), "--skip-fab-export"])
    assert rc == 1


def test_build_release_fails_gate_when_pcb_modified_after_sentinel(tmp_path):
    proj = _make_fixture_project(tmp_path)
    sentinel_path = proj / "datasheets" / ".bom_readiness.json"
    s = json.loads(sentinel_path.read_text())
    s["verified_at"] = "2000-01-01T00:00:00Z"  # very old
    sentinel_path.write_text(json.dumps(s))

    pcb = proj / "kicad" / "fixture_proj.kicad_pcb"
    time.sleep(0.05)
    os.utime(pcb, None)

    rc = build_release.main([str(proj), "--skip-fab-export"])
    assert rc == 1
