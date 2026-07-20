"""End-to-end test of the `setup` extraction against a fake Windows tree.

Runs the real script unprivileged via the SL7_SENSORS_STATE override
(scratch state dir, no mounting — the Windows root is passed explicitly —
and no service activation). Pins the two extraction bugs that mattered:
the DriverStore glob must match directories only (same-named .ini FILES
sit next to package dirs and would win newest-by-mtime), and CRLF must be
stripped from everything the DSP parses line-wise.
"""
import os
import pathlib
import subprocess
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SETUP = ROOT / "setup"


def make_windows_root(tmp_path):
    repo = tmp_path / "win/Windows/System32/DriverStore/FileRepository"
    repo.mkdir(parents=True)
    return tmp_path / "win", repo


def make_pkg(repo, name, marker, mtime):
    pkg = repo / name
    pkg.mkdir()
    (pkg / "sns_reg_config").write_bytes(
        b"registry_dir=/persist/sensors\r\nmarker=" + marker + b"\r\n")
    (pkg / "json.lst").write_bytes(b"one.json\r\ntwo.json\r\n")
    (pkg / "cfg_lc.json").write_bytes(b'{"marker": "' + marker + b'"}\r\n')
    (pkg / "hw_platform").write_bytes(b"LC\r\n")
    (pkg / "soc_id").write_bytes(b"557\r\n")
    (pkg / "driver.inf").write_bytes(b"[Version]\r\n")
    (pkg / "driver.cat").write_bytes(b"\x00sig")
    os.utime(pkg, (mtime, mtime))
    return pkg


def run_setup(winroot, state):
    return subprocess.run(
        ["bash", str(SETUP), str(winroot)],
        env={**os.environ, "SL7_SENSORS_STATE": str(state)},
        capture_output=True, text=True, cwd=str(ROOT))


@pytest.fixture
def extracted(tmp_path):
    winroot, repo = make_windows_root(tmp_path)
    now = time.time()
    make_pkg(repo, "qcom_snscfg.inf_arm64_old0", b"OLD", now - 1000)
    make_pkg(repo, "qcom_snscfg.inf_arm64_new1", b"NEW", now - 10)
    # the trap: same-named FILES next to the package dirs, newest of all —
    # a glob without the trailing slash picks one of these
    ini = repo / "qcom_snscfg.inf_arm64_zzz.ini"
    ini.write_bytes(b"[ini]\r\n")
    os.utime(ini, (now, now))
    bare = repo / "qcom_snscfg.inf_arm64_bare"
    bare.write_bytes(b"not a dir")
    os.utime(bare, (now, now))
    state = tmp_path / "state"
    result = run_setup(winroot, state)
    assert result.returncode == 0, result.stderr
    return state / "sns-config", result


def test_picks_newest_package_directory_never_a_file(extracted):
    serve, result = extracted
    reg = (serve / "vendor/etc/sensors/sns_reg_config").read_bytes()
    assert b"marker=NEW" in reg
    assert "multiple snscfg packages" in result.stdout


def test_serving_tree_layout(extracted):
    serve, _ = extracted
    assert (serve / "vendor/etc/sensors/sns_reg_config").is_file()
    assert (serve / "vendor/etc/sensors/config/json.lst").is_file()
    assert (serve / "vendor/etc/sensors/config/cfg_lc.json").is_file()
    assert (serve / "sys/devices/soc0/soc_id").is_file()
    assert (serve / "sys/devices/soc0/hw_platform").is_file()
    assert (serve / "persist/sensors/registry/registry").is_dir()


def test_inf_and_cat_excluded(extracted):
    serve, _ = extracted
    cfg = serve / "vendor/etc/sensors/config"
    assert not list(cfg.glob("*.inf"))
    assert not list(cfg.glob("*.cat"))


def test_crlf_stripped_everywhere_line_parsed(extracted):
    serve, _ = extracted
    for rel in ("vendor/etc/sensors/sns_reg_config",
                "vendor/etc/sensors/config/json.lst",
                "vendor/etc/sensors/config/cfg_lc.json",
                "sys/devices/soc0/soc_id"):
        assert b"\r" not in (serve / rel).read_bytes(), rel


def test_world_readable(extracted):
    serve, _ = extracted
    mode = (serve / "vendor/etc/sensors/sns_reg_config").stat().st_mode
    assert mode & 0o004


def test_only_sibling_files_means_no_package(tmp_path):
    winroot, repo = make_windows_root(tmp_path)
    (repo / "qcom_snscfg.inf_arm64_zzz.ini").write_bytes(b"[ini]")
    result = run_setup(winroot, tmp_path / "state")
    assert result.returncode != 0
    assert "no *snscfg* driver package" in result.stderr


def test_failed_extraction_keeps_old_tree_and_leaves_no_staging(tmp_path):
    winroot, repo = make_windows_root(tmp_path)
    pkg = make_pkg(repo, "qcom_snscfg.inf_arm64_bad", b"BAD", time.time())
    (pkg / "sns_reg_config").unlink()          # triggers the die() mid-build
    state = tmp_path / "state"
    old = state / "sns-config/vendor/etc/sensors"
    old.mkdir(parents=True)
    (old / "sns_reg_config").write_text("previous working config\n")
    result = run_setup(winroot, state)
    assert result.returncode != 0
    assert "no sns_reg_config" in result.stderr
    # the working tree survives, the staging dir does not
    assert (old / "sns_reg_config").read_text() == "previous working config\n"
    assert not list(state.glob("sns-config.new.*"))


def test_rejects_non_windows_root(tmp_path):
    (tmp_path / "empty").mkdir()
    result = run_setup(tmp_path / "empty", tmp_path / "state")
    assert result.returncode != 0
    assert "does not look like a Windows root" in result.stderr
