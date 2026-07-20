"""BacklightController math: anchoring, the compressed-domain floor,
exponential approach with 1-count minimum steps, and external-write
adoption. Instances are built directly (no /sys, no state file) with a
dict-backed fake sysfs."""
import json
import math

import pytest


BL_MAX = 4095


@pytest.fixture
def ctrl(sp):
    c = sp.BacklightController.__new__(sp.BacklightController)
    c.proxy = None
    c.enabled = True
    c.last_comp = None
    c.bl_path = "/nonexistent"
    c.bl_max = BL_MAX
    c.state_dir = "/nonexistent"
    c.state_file = "/nonexistent/anchor.json"
    c.k = None
    c.target = None
    c.last_written = None
    c.writable = False              # dry run: _tick tracks last_written only
    c._save_state = lambda: None
    c.sysfs = {"brightness": 2000}
    c._read = lambda attr: str(c.sysfs[attr])
    return c


# --- on_light: anchoring and targeting ---

def test_first_light_adopts_current_brightness_as_anchor(ctrl):
    ctrl.on_light(4.0)
    assert ctrl.k == pytest.approx((2000 / BL_MAX) / 4.0)
    assert ctrl.target == pytest.approx(2000 / BL_MAX)


def test_initial_anchor_respects_min_fraction(sp, ctrl):
    ctrl.sysfs["brightness"] = 10        # below CTRL_MIN_FRAC of range
    ctrl.on_light(4.0)
    assert ctrl.k == pytest.approx(sp.CTRL_MIN_FRAC / 4.0)


def test_target_clamped_to_full_range(sp, ctrl):
    ctrl.k = 0.12
    ctrl.on_light(100.0)
    assert ctrl.target == 1.0
    ctrl.on_light(1.0)                   # floor also floors the target
    assert ctrl.target == pytest.approx(max(sp.CTRL_MIN_FRAC, 0.12))


def test_comp_floor_clamps_noise_input(sp, ctrl):
    # below ~1 lux the sensor is noise: anchor and target must both see
    # the floor, so darkness behaves as plain manual control
    ctrl.k = 0.5
    ctrl.on_light(0.01)
    assert ctrl.last_comp == sp.CTRL_COMP_FLOOR
    assert ctrl.target == pytest.approx(0.5 * sp.CTRL_COMP_FLOOR)


# --- _tick: the ramp ---

def test_large_gap_capped_at_ramp_step(sp, ctrl):
    ctrl.on_light(4.0)                   # first call only anchors
    ctrl.on_light(20.0)                  # lamp on: big jump upward
    assert ctrl.target == 1.0
    ctrl._tick()
    step = int(round(sp.CTRL_RAMP_STEP * BL_MAX))
    assert ctrl.last_written == 2000 + step


def test_small_gap_glides_in_single_counts(ctrl):
    ctrl.k = 1.0                         # irrelevant; target set directly
    ctrl.target = 2005 / BL_MAX
    ctrl._tick()
    assert ctrl.last_written == 2001     # never a visible jump


def test_within_stop_counts_writes_nothing(ctrl):
    ctrl.target = 2001 / BL_MAX
    ctrl._tick()
    assert ctrl.last_written is None


def test_tick_backs_off_when_someone_else_moved_the_backlight(ctrl):
    ctrl.target = 1.0
    ctrl.last_written = 2082
    ctrl.sysfs["brightness"] = 3000      # slider/keys/idle-dim
    ctrl._tick()
    assert ctrl.last_written == 2082     # no write, no fight


def test_tick_idle_without_target(ctrl):
    assert ctrl._tick() is True
    assert ctrl.last_written is None


# --- _poll_user: adopting external writes ---

def test_user_write_becomes_new_anchor(ctrl):
    ctrl.k = 0.1
    ctrl.target = 0.5
    ctrl.last_written = 2082
    ctrl.last_comp = 4.0
    ctrl.sysfs["brightness"] = 3000
    ctrl._poll_user()
    assert ctrl.k == pytest.approx((3000 / BL_MAX) / 4.0)
    assert ctrl.target == pytest.approx(3000 / BL_MAX)
    assert ctrl.last_written == 3000


def test_own_write_is_not_adopted(ctrl):
    ctrl.k = 0.1
    ctrl.target = 0.5
    ctrl.last_written = 2082
    ctrl.last_comp = 4.0
    ctrl.sysfs["brightness"] = 2082
    ctrl._poll_user()
    assert ctrl.k == 0.1


def test_zero_write_is_screen_blanking_not_intent(ctrl):
    ctrl.k = 0.1
    ctrl.target = 0.5
    ctrl.last_written = 2082
    ctrl.last_comp = 4.0
    ctrl.sysfs["brightness"] = 0         # DPMS/lock
    ctrl._poll_user()
    assert ctrl.k == 0.1                 # anchoring on 0 would slam k down


def test_write_within_deadband_of_target_ignored(ctrl):
    ctrl.k = 0.1
    ctrl.target = 2050 / BL_MAX
    ctrl.last_written = 2082
    ctrl.last_comp = 4.0
    ctrl.sysfs["brightness"] = 2050      # mid-ramp wobble, not a decision
    ctrl._poll_user()
    assert ctrl.k == 0.1


# --- _load_state: corrupt state must never poison the controller ---

def load_k(sp, tmp_path, content):
    c = sp.BacklightController.__new__(sp.BacklightController)
    c.k = None
    c.state_file = str(tmp_path / "anchor.json")
    if content is not None:
        (tmp_path / "anchor.json").write_text(content)
    c._load_state()
    return c.k


def test_load_state_valid(sp, tmp_path):
    assert load_k(sp, tmp_path, json.dumps({"k": 0.5})) == 0.5


@pytest.mark.parametrize("content", [
    None,                        # missing file
    "garbage",                   # not JSON
    "null",                      # valid JSON, wrong shape (TypeError)
    "[1, 2]",                    # valid JSON, wrong shape
    '{"nope": 1}',               # missing key
    '{"k": "0.5"}',              # wrong type
    '{"k": NaN}',                # NaN: k == k is False
    '{"k": 0}',                  # zero anchor is degenerate
    '{"k": -3}',                 # negative
    '{"k": 2e6}',                # absurdly large
])
def test_load_state_rejects_corrupt(sp, tmp_path, content):
    assert load_k(sp, tmp_path, content) is None


def test_state_roundtrip(sp, tmp_path):
    c = sp.BacklightController.__new__(sp.BacklightController)
    c.k = 0.1234
    c.state_dir = str(tmp_path)
    c.state_file = str(tmp_path / "anchor.json")
    c._save_state()
    c2 = sp.BacklightController.__new__(sp.BacklightController)
    c2.k = None
    c2.state_file = c.state_file
    c2._load_state()
    assert c2.k == pytest.approx(0.1234)
    assert not math.isnan(c2.k)
