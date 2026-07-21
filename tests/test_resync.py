"""ResyncWatcher: divergence detection (including mid-ramp), the blanking
rule, entry picking, and the stale-serial retry. Instances are built with
injected fakes — no sysfs, no session bus. The fake set_backlight mirrors
mutter: a successful call updates the cached state."""
import functools

import pytest


BL_MAX = 4095
CONNECTOR = "eDP-1"


def entry(value, connector=CONNECTOR, active=True, maximum=BL_MAX):
    return {"connector": connector, "active": active,
            "min": 40, "max": maximum, "value": value}


class Fixture:
    """Dict-backed sysfs + recorder; set_backlight updates the cache."""

    def __init__(self, br, brightness, cached):
        self.error = br.GLib.Error
        self.brightness = brightness
        self.state = (2, [entry(cached)])
        self.calls = []
        self.set_error = 0          # raise on this many leading set calls
        self.watcher = br.ResyncWatcher(
            lambda: self.brightness,
            lambda: self.state,
            self.set_backlight,
            bl_max=BL_MAX)

    def set_backlight(self, serial, connector, value):
        if self.set_error > 0:
            self.set_error -= 1
            raise self.error("stale serial")
        self.calls.append((serial, connector, value))
        self.state = (serial, [entry(value, connector=connector)])


@pytest.fixture
def fx(br):
    return functools.partial(Fixture, br)


def test_constants_stay_matched_with_controller(sp, br):
    # comment-coupled across two shipped executables; pin the invariants:
    # same poll cadence, and slack >= the controller's stop deadband so its
    # settling can never re-trigger a sync
    assert br.RESYNC_POLL_S == sp.CTRL_USER_POLL_S
    assert br.RESYNC_SLACK_COUNTS >= sp.CTRL_STOP_COUNTS


def test_within_slack_does_not_sync(br, fx):
    f = fx(345 + br.RESYNC_SLACK_COUNTS, 345)
    for _ in range(3):
        f.watcher.poll()
    assert f.calls == []


def test_diverged_syncs_once_then_cache_tracks(br, fx):
    f = fx(1900, 345)
    f.watcher.poll()
    assert f.calls == [(2, CONNECTOR, 1900)]
    for _ in range(5):                  # cache now matches: no re-calls
        f.watcher.poll()
    assert len(f.calls) == 1


def test_mid_ramp_syncs_every_poll(br, fx):
    # a long ramp must not starve the sync: each poll pushes the current
    # value so the cache (and the GNOME slider) is never more than one
    # poll behind
    f = fx(1000, 345)
    for step in range(5):
        f.brightness = 1000 + 100 * step
        f.watcher.poll()
    assert [c[2] for c in f.calls] == [1000, 1100, 1200, 1300, 1400]


def test_zero_is_blanking_not_brightness(br, fx):
    f = fx(0, 345)
    for _ in range(3):
        f.watcher.poll()
    assert f.calls == []


def test_mutter_absent_is_quiet(br, fx):
    f = fx(1900, 345)
    f.watcher.get_backlight = lambda: None
    f.watcher.poll()
    assert f.calls == []


def test_inactive_entry_skipped(br, fx):
    f = fx(1900, 345)
    f.state = (2, [entry(345, active=False)])
    f.watcher.poll()
    assert f.calls == []


def test_entry_matching_sysfs_range_preferred(br, fx):
    f = fx(1900, 345)
    f.state = (2, [entry(50, connector="DP-3", maximum=100), entry(345)])
    f.watcher.poll()
    assert f.calls == [(2, CONNECTOR, 1900)]


def test_stale_serial_refetches_and_retries_once(br, fx):
    f = fx(1900, 345)
    f.set_error = 1                 # first SetBacklight rejects the serial
    serials = iter([2, 3])          # monitor reconfig between get and set
    f.watcher.get_backlight = lambda: (next(serials, 3), [entry(345)])
    f.watcher.poll()
    assert f.calls == [(3, CONNECTOR, 1900)]


def test_double_failure_holds_until_next_change(br, fx):
    f = fx(1900, 345)
    f.set_error = 99                # every call fails
    f.watcher.poll()
    assert 99 - f.set_error == 2    # first try + one retry, then give up
    for _ in range(5):              # held: no further attempts at this value
        f.watcher.poll()
    assert 99 - f.set_error == 2
    f.set_error = 0                 # a new value lifts the hold
    f.brightness = 2100
    f.watcher.poll()
    assert f.calls == [(2, CONNECTOR, 2100)]
