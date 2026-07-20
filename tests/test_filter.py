"""The dual-tau EMA in SensorProxy.filter_sample: slow smoothing for
ambient drift, switching to the fast tau only when a large deviation
persists for FAST_HOLD_S."""
import pytest


class FakeGLib:
    """Deterministic clock; idle_add swallowed (no main loop in tests)."""

    def __init__(self):
        self.now = 100.0

    def get_monotonic_time(self):
        return int(self.now * 1e6)

    @staticmethod
    def idle_add(*args, **kwargs):
        return 0


@pytest.fixture
def clock(sp, monkeypatch):
    fake = FakeGLib()
    monkeypatch.setattr(sp, "GLib", fake)
    return fake


def feed(sp, clock, samples, rate=4.0):
    """Run samples through the filter, returning [(ema, fast_since), ...]."""
    proxy = sp.SensorProxy()
    ema = fast_since = None
    out = []
    for lux in samples:
        clock.now += 1.0 / rate
        ema, fast_since = proxy.filter_sample(lux, ema, fast_since, rate)
        out.append((ema, fast_since))
    return out


def alphas(out, target):
    """Effective per-sample smoothing factor between consecutive EMAs."""
    return [(b[0] - a[0]) / (target - a[0]) for a, b in zip(out, out[1:])]


def test_first_sample_seeds_ema(sp, clock):
    assert feed(sp, clock, [123.4])[0] == (123.4, None)


def test_small_deviation_uses_slow_tau(sp, clock):
    rate = 4.0
    out = feed(sp, clock, [100.0, 104.0], rate)
    dt = 1.0 / rate
    expected_alpha = dt / (sp.TAU_SLOW + dt)
    assert out[1][0] == pytest.approx(100.0 + expected_alpha * 4.0)
    assert out[1][1] is None                 # deviation below FAST_TRIGGER


def test_sustained_deviation_switches_to_fast_tau(sp, clock):
    rate = 4.0
    dt = 1.0 / rate
    out = feed(sp, clock, [100.0] + [300.0] * 12, rate)
    a = alphas(out, 300.0)
    slow = dt / (sp.TAU_SLOW + dt)
    fast = dt / (sp.TAU_FAST + dt)
    # the jump itself starts the hold timer but still smooths slowly
    assert a[0] == pytest.approx(slow)
    assert out[1][1] is not None
    # once the deviation has persisted FAST_HOLD_S, tau drops
    held = int(sp.FAST_HOLD_S / dt)
    assert a[held] == pytest.approx(fast)
    assert fast > 3 * slow                   # the switch is what makes lamp
    #                                          on/off feel responsive


def test_transient_spike_does_not_arm_fast_mode(sp, clock):
    out = feed(sp, clock, [100.0, 300.0, 105.0], 4.0)
    assert out[1][1] is not None             # spike armed the timer
    assert out[2][1] is None                 # returning to baseline reset it


def test_near_zero_ema_does_not_divide_by_zero(sp, clock):
    out = feed(sp, clock, [0.0, 0.5, 0.0], 4.0)
    assert all(e >= 0.0 for e, _ in out)
