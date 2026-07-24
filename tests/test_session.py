"""Active-session tracking: the controller stands down while a display
manager greeter owns the seat (the greeter runs its own auto-brightness on
the same panel). The lookup is a two-hop async logind query — seat0's
ActiveSession, then that session's Class — so the bus here is a stub that
records calls and lets the test deliver each reply."""
import pytest

import gi
gi.require_version("GLib", "2.0")
from gi.repository import GLib          # noqa: E402


SEAT_REPLY = GLib.Variant("(v)", (GLib.Variant(
    "(so)", ("2", "/org/freedesktop/login1/session/_32")),))


class FakeBus:
    """Stands in for the system bus connection: records async calls and
    hands the recorded reply straight back from call_finish."""

    def __init__(self):
        self.calls = []

    def call(self, dest, path, iface, method, params, reply_type, flags,
             timeout, cancellable, callback):
        self.calls.append({"path": path, "params": params,
                           "callback": callback})

    def call_finish(self, res):
        return res

    def reply(self, variant):
        """Deliver a reply to the most recent call."""
        self.calls[-1]["callback"](self, variant)


class FakeController:
    def __init__(self):
        self.enabled = True
        self.greeter_calls = []

    def set_greeter(self, on):
        self.greeter_calls.append(bool(on))


@pytest.fixture
def proxy(sp):
    p = sp.SensorProxy()            # real defaults; only the bus is faked
    p.controller = FakeController()
    p.logind = FakeBus()
    return p


def test_greeter_class_stands_the_controller_down(proxy):
    proxy.refresh_active_session()
    proxy.logind.reply(SEAT_REPLY)                  # ActiveSession
    proxy.logind.reply(GLib.Variant("(v)", (GLib.Variant("s", "greeter"),)))
    assert proxy.controller.greeter_calls == [True]


def test_user_class_leaves_the_controller_running(proxy):
    proxy.refresh_active_session()
    proxy.logind.reply(SEAT_REPLY)
    proxy.logind.reply(GLib.Variant("(v)", (GLib.Variant("s", "user"),)))
    assert proxy.controller.greeter_calls == [False]


def test_query_runs_before_the_controller_starts(proxy):
    # regression: gating this on controller.enabled dropped the greeter's
    # SessionNew (~0.3 s into boot) because the controller only starts once
    # the sensor is discovered ~2 s in — the greeter then went unnoticed
    proxy.controller.enabled = False
    proxy.refresh_active_session()
    assert proxy.logind.calls, "no seat query issued before controller start"


def test_no_controller_means_no_query(proxy):
    proxy.controller = None                         # desktop mode
    proxy.refresh_active_session()
    assert proxy.logind.calls == []


def test_empty_seat_leaves_the_controller_running(proxy):
    # no active session (headless, or between sessions): never stand down
    proxy.refresh_active_session()
    proxy.logind.reply(GLib.Variant("(v)", (GLib.Variant("(so)", ("", "/")),)))
    assert proxy.controller.greeter_calls == [False]
    assert len(proxy.logind.calls) == 1             # no Class hop needed


def test_vanished_session_leaves_the_controller_running(sp, proxy):
    # the session died between the two hops: GLib.Error out of call_finish
    proxy.refresh_active_session()
    proxy.logind.reply(SEAT_REPLY)

    def boom(res):
        raise GLib.Error("gone")

    proxy.logind.call_finish = boom
    proxy.logind.reply(None)
    assert proxy.controller.greeter_calls == [False]
