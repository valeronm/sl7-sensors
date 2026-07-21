"""Load the extensionless executables as importable modules.

sensor-proxy and read-sensor are shipped as scripts, not a package; the
SourceFileLoader shim below imports them by path so their pure functions
(protobuf/QMI framing, EMA filter, controller math) are unit-testable
without restructuring the repo. Neither script touches sockets, D-Bus or
sysfs at import time.
"""
import importlib.machinery
import importlib.util
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _load(modname, filename):
    if modname in sys.modules:
        return sys.modules[modname]
    saved_argv = sys.argv
    sys.argv = [filename]           # read-sensor peeks at argv at import time
    try:
        loader = importlib.machinery.SourceFileLoader(
            modname, str(ROOT / filename))
        spec = importlib.util.spec_from_loader(modname, loader)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        loader.exec_module(mod)
    finally:
        sys.argv = saved_argv
    return mod


@pytest.fixture(scope="session")
def sp():
    pytest.importorskip("gi")       # sensor-proxy imports GLib at module level
    return _load("sensor_proxy", "sensor-proxy")


@pytest.fixture(scope="session")
def rs():
    return _load("read_sensor", "read-sensor")


@pytest.fixture(scope="session")
def br():
    pytest.importorskip("gi")       # backlight-resync imports Gio/GLib too
    return _load("backlight_resync", "backlight-resync")
