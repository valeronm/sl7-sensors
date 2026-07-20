"""Dispatcher exit codes and usage text — the paths that need no
/usr/lib/sl7-sensors internals."""
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "sl7-sensors"


def run(*args):
    return subprocess.run(["bash", str(CLI), *args],
                          capture_output=True, text=True)


def test_no_args_is_an_error_with_usage():
    r = run()
    assert r.returncode == 1
    assert "usage: sl7-sensors" in r.stdout


def test_help_exits_zero():
    for arg in ("-h", "--help", "help"):
        r = run(arg)
        assert r.returncode == 0, arg
        assert "usage: sl7-sensors" in r.stdout


def test_unknown_command_names_itself():
    r = run("frobnicate")
    assert r.returncode == 1
    assert "sl7-sensors: unknown command: frobnicate" in r.stderr
