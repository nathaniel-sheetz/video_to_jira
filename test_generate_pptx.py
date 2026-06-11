"""
Tests for generate_pptx.py — CLI wiring (--project, --config, empty-config exit).

Run either way:
    python -m pytest test_generate_pptx.py
    python test_generate_pptx.py
"""

import json
import os
import tempfile

import pytest
import generate_pptx


def test_main_exits_when_config_not_found():
    # --config to a nonexistent file → load_config returns ({}, path) → sys.exit "Config not found"
    with pytest.raises(SystemExit) as exc:
        generate_pptx.main(["--config", "does_not_exist_xyz_42.json"])
    assert "Config not found" in str(exc.value)


def test_main_project_flag_loads_project_config():
    # --project <name> resolves to projects/<name>/config.json.
    # A valid but incomplete config (issues_path points to a nonexistent file) reaches
    # "File not found" rather than "Config not found", proving the config was loaded.
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            proj_dir = os.path.join("projects", "test-pptx-session")
            os.makedirs(proj_dir)
            cfg_data = {
                "issues_path": "no_such_issues.json",
                "selections_path": "no_selections.json",
                "template_path": "no_template.pptx",
                "output_pptx": "out.pptx",
            }
            with open(os.path.join(proj_dir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg_data, f)
            with pytest.raises(SystemExit) as exc:
                generate_pptx.main(["--project", "test-pptx-session"])
            # "File not found" means config was loaded successfully (not "Config not found")
            assert "File not found" in str(exc.value)
        finally:
            os.chdir(cwd)


def test_main_config_flag_loads_explicit_path():
    # --config <path> loads that specific file.
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            cfg_path = os.path.join(d, "explicit_config.json")
            cfg_data = {
                "issues_path": "no_such_issues.json",
                "selections_path": "no_selections.json",
                "template_path": "no_template.pptx",
                "output_pptx": "out.pptx",
            }
            with open(cfg_path, "w", encoding="utf-8") as f:
                json.dump(cfg_data, f)
            with pytest.raises(SystemExit) as exc:
                generate_pptx.main(["--config", cfg_path])
            assert "File not found" in str(exc.value)
        finally:
            os.chdir(cwd)


if __name__ == "__main__":
    import traceback
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            passed += 1
            print(f"  PASS  {name}")
        except Exception:
            failed += 1
            print(f"  FAIL  {name}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    raise SystemExit(1 if failed else 0)
