"""Suspend/resume stream handling: suspend_stream must bypass the
controller guard (an open SSC stream wakes the sleeping SoC every push),
and resume must respect claim semantics. Instances are built directly;
no D-Bus — logind is left unset so inhibitor paths are no-ops."""
import threading

import pytest


@pytest.fixture
def proxy(sp):
    p = sp.SensorProxy.__new__(sp.SensorProxy)
    p.controller = object()          # controller mode: stop_stream refuses
    p.claimers = {}
    p.logind = None
    p.sleep_inhibitor = None
    p.suid = None                    # keeps start_stream a no-op
    p.stream_stop = threading.Event()
    p.stream_thread = threading.Thread(target=lambda: None)
    p.stream_thread.start()
    return p


def test_stop_stream_keeps_controller_stream(proxy):
    stop = proxy.stream_stop
    proxy.stop_stream()
    assert not stop.is_set()
    assert proxy.stream_thread is not None


def test_suspend_stream_overrides_controller_guard(proxy):
    stop = proxy.stream_stop
    proxy.suspend_stream()
    assert stop.is_set()
    assert proxy.stream_thread is None
    assert proxy.stream_stop is None


def test_prepare_for_sleep_entering_releases_stream(sp, proxy):
    class Params:
        @staticmethod
        def unpack():
            return (True,)
    stop = proxy.stream_stop
    proxy.on_prepare_for_sleep(None, None, None, None, None, Params, None)
    assert stop.is_set()
    assert proxy.stream_thread is None


def test_prepare_for_sleep_resume_restarts_only_when_wanted(sp, proxy):
    class Params:
        @staticmethod
        def unpack():
            return (False,)
    started = []
    proxy.start_stream = lambda: started.append(True)

    proxy.controller = None
    proxy.stream_thread = None
    proxy.on_prepare_for_sleep(None, None, None, None, None, Params, None)
    assert not started                # no controller, no claimers

    proxy.claimers = {":1.42": (1, 0)}
    proxy.on_prepare_for_sleep(None, None, None, None, None, Params, None)
    assert started                    # claimer present -> resubscribe
