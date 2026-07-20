"""set_mode: the /etc/default ARGS rewrite behind 'sl7-sensors mode'.

Regex-rewrite logic with an append-when-deleted branch — exactly the kind
of thing that regresses silently. Runs against a tmp file with geteuid and
systemctl stubbed out.
"""
import pytest


@pytest.fixture
def env(sp, tmp_path, monkeypatch):
    default = tmp_path / "sl7-sensor-proxy"
    default.write_text("# comment kept intact\nARGS=\n")
    monkeypatch.setattr(sp, "DEFAULT_FILE", str(default))
    monkeypatch.setattr(sp.os, "geteuid", lambda: 0)
    calls = []
    monkeypatch.setattr("subprocess.run",
                        lambda cmd, **kw: calls.append(cmd) or None)
    return default, calls


def test_controller_rewrites_args_and_restarts(sp, env, capsys):
    default, calls = env
    sp.set_mode("controller")
    assert "ARGS=--controller\n" in default.read_text()
    assert "# comment kept intact" in default.read_text()
    assert ["systemctl", "restart", "sl7-sensor-proxy.service"] in calls
    assert "ambient-enabled false" in capsys.readouterr().out


def test_desktop_clears_args(sp, env, capsys):
    default, _ = env
    default.write_text("ARGS=--controller\n")
    sp.set_mode("desktop")
    assert "ARGS=\n" in default.read_text()
    assert "ambient-enabled true" in capsys.readouterr().out


def test_missing_args_line_is_appended_not_silently_dropped(sp, env):
    default, _ = env
    default.write_text("# ARGS line deleted by hand\n")
    sp.set_mode("controller")
    assert default.read_text().endswith("ARGS=--controller\n")


def test_invalid_mode_exits(sp, env):
    with pytest.raises(SystemExit, match="usage"):
        sp.set_mode("gnome")


def test_requires_root(sp, env, monkeypatch):
    monkeypatch.setattr(sp.os, "geteuid", lambda: 1000)
    with pytest.raises(SystemExit, match="root"):
        sp.set_mode("controller")


def test_unreadable_default_file_exits(sp, env, monkeypatch):
    monkeypatch.setattr(sp, "DEFAULT_FILE", "/nonexistent/default")
    with pytest.raises(SystemExit, match="cannot read"):
        sp.set_mode("controller")
