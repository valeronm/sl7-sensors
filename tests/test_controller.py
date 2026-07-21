"""BacklightController math: the multi-point calibration curve
(evaluation, warping, monotone repair), the compressed-domain floor,
exponential approach with 1-count minimum steps, and deferred adoption of
external writes. Instances are built directly (no /sys, no state file)
with a dict-backed fake sysfs."""
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
    c.curve = None
    c.target = None
    c.last_written = None
    c.tick_seq = 0
    c.recent_writes = []
    c.pending = None
    c.writable = False              # dry run: _tick tracks last_written only
    c._save_state = lambda: None
    c.sysfs = {"brightness": 2000}
    c._read = lambda attr: str(c.sysfs[attr])
    return c


# --- curve_eval: interpolation and the origin-ray ends ---

def test_eval_single_point_is_the_k_law(sp):
    # the migrated anchor [[1, k]] must reproduce k*comp exactly everywhere
    for comp in (1.0, 2.7, 50.0):
        assert sp.curve_eval([[1.0, 0.252]], comp) == 0.252 * comp


def test_eval_interpolates_between_points(sp):
    assert sp.curve_eval([[1.0, 0.1], [15.0, 0.9]], 8.0) == \
        pytest.approx(0.1 + 0.8 * 7 / 14)


def test_eval_below_first_point_takes_origin_ray(sp):
    # seed at comp 10: darker light scales down proportionally, k-law style
    assert sp.curve_eval([[10.0, 0.5]], 3.0) == pytest.approx(0.15)


def test_eval_above_last_point_takes_origin_ray_not_segment(sp):
    # a shallow night-side segment must not cap daylight: after a night dim
    # to [(1, .048), (1.2, .05)], daylight (comp 20) follows the origin ray
    # through the last point (~0.83), not the last segment's slope (~0.24)
    pts = [[1.0, 0.048], [1.2, 0.05]]
    assert sp.curve_eval(pts, 20.0) == pytest.approx(20 * 0.05 / 1.2)
    assert sp.curve_eval(pts, 20.0) > 0.8


# --- curve_warp: locality, merge, monotonicity, budget ---

def test_warp_is_local_in_log_light(sp):
    # correcting at ~2 lux leaves a point 100x away bit-identical
    pts = [[1.0, 0.1], [15.0, 0.9]]
    new = sp.curve_warp(pts, 1.5, 0.3)
    assert [15.0, 0.9] in new
    assert [1.5, 0.3] in new
    # the nearby point moves by factor**w: factor = .3/eval(1.5), w = .797
    factor = 0.3 / sp.curve_eval(pts, 1.5)
    w = 1 - math.log(1.5) / sp.CTRL_WARP_RADIUS_LN
    near = [p for p in new if p[0] == 1.0][0]
    assert near[1] == pytest.approx(0.1 * factor ** w)


def test_warp_dim_respects_min_fraction(sp):
    new = sp.curve_warp([[1.2, 0.03]], 1.0, sp.CTRL_MIN_FRAC)
    assert all(f >= sp.CTRL_MIN_FRAC for _, f in new)


def test_warp_merges_nearby_point(sp):
    # within the merge band the correction replaces, never stacks
    pts = [[2.0, 0.5], [15.0, 0.9]]
    new = sp.curve_warp(pts, 2.1, 0.6)     # ln(2.1/2.0) = 0.049 < 0.1
    assert len(new) == 2
    assert [2.1, 0.6] in new


def test_warp_monotone_repair_clamps_to_correction(sp):
    # "bright at night" implies "at least as bright in daylight": the
    # daylight point is clamped to exactly the corrected value, not dropped
    new = sp.curve_warp([[10.0, 0.8]], 1.5, 0.9)
    assert new == [[1.5, 0.9], [10.0, 0.9]]


def test_warp_repairs_reorder_without_insert_conflict(sp):
    # dimming warps nearer points harder; a shallow pair can reorder and
    # must come back monotone
    new = sp.curve_warp([[2.0, 0.5], [2.5, 0.52]], 3.0, 0.2)
    fracs = [f for _, f in new]
    assert fracs == sorted(fracs)
    assert [3.0, 0.2] in new


def test_warp_enforces_point_budget(sp):
    pts = [[float(1.3 ** i), 0.05 + 0.1 * i] for i in range(8)]
    new = sp.curve_warp(pts, 100.0, 1.0)
    assert len(new) <= sp.CTRL_MAX_POINTS
    assert [100.0, 1.0] in new
    assert new[0] == pts[0]                # endpoints survive


# --- on_light: seeding and targeting ---

def test_first_light_seeds_curve_at_current_brightness(ctrl):
    ctrl.on_light(4.0)
    assert ctrl.curve == [[4.0, pytest.approx(2000 / BL_MAX)]]
    assert ctrl.target == pytest.approx(2000 / BL_MAX)


def test_seed_respects_min_fraction(sp, ctrl):
    ctrl.sysfs["brightness"] = 10        # below CTRL_MIN_FRAC of range
    ctrl.on_light(4.0)
    assert ctrl.curve[0][1] == sp.CTRL_MIN_FRAC


def test_target_clamped_to_full_range(sp, ctrl):
    ctrl.curve = [[1.0, 0.12]]
    ctrl.on_light(100.0)
    assert ctrl.target == 1.0
    ctrl.on_light(1.0)                   # floor also floors the target
    assert ctrl.target == pytest.approx(max(sp.CTRL_MIN_FRAC, 0.12))


def test_comp_floor_clamps_noise_input(sp, ctrl):
    # below ~1 lux the sensor is noise: darkness behaves as plain manual
    # control at the curve's low end
    ctrl.curve = [[1.0, 0.5]]
    ctrl.on_light(0.01)
    assert ctrl.last_comp == sp.CTRL_COMP_FLOOR
    assert ctrl.target == pytest.approx(0.5)


# --- _tick: the ramp ---

def test_large_gap_capped_at_ramp_step(sp, ctrl):
    ctrl.on_light(4.0)                   # first call only seeds
    ctrl.on_light(20.0)                  # lamp on: big jump upward
    assert ctrl.target == 1.0
    ctrl._tick()
    step = int(round(sp.CTRL_RAMP_STEP * BL_MAX))
    assert ctrl.last_written == 2000 + step


def test_small_gap_glides_in_single_counts(ctrl):
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


# --- _poll_user: deferred adoption of external writes ---

def start_pending(ctrl, brightness=3000):
    """Seed, then simulate a user write and the poll that detects it."""
    ctrl.curve = [[1.0, 0.25]]
    ctrl.on_light(4.0)
    ctrl.last_written = 2000
    ctrl.sysfs["brightness"] = brightness
    ctrl._poll_user()
    return brightness / BL_MAX


def test_user_write_freezes_target_and_defers(ctrl):
    frac = start_pending(ctrl)
    assert ctrl.pending["frac"] == pytest.approx(frac)
    assert ctrl.target == pytest.approx(frac)
    assert ctrl.last_written == 3000
    assert ctrl.curve == [[1.0, 0.25]]   # not yet committed


def test_correction_during_falling_light_does_not_decay(sp, ctrl):
    # THE observed defect: correct while the EMA is still falling; the old
    # code re-derived the law from the transient reading and the target
    # kept sliding below the user's choice
    frac = start_pending(ctrl)
    for comp in (3.0, 2.2, 2.0, 2.0, 2.0, 2.0, 2.0, 2.0):
        ctrl.on_light(comp)
        ctrl._poll_user()
        assert ctrl.target == pytest.approx(frac)   # frozen, no decay
    assert ctrl.pending is None                # committed
    assert any(x == 2.0 for x, _ in ctrl.curve)     # at the SETTLED comp
    ctrl.on_light(2.0)
    assert ctrl.target == pytest.approx(frac)       # no jump at unfreeze


def test_stable_light_commits_after_settle_polls(sp, ctrl):
    start_pending(ctrl)
    for i in range(sp.CTRL_SETTLE_POLLS - 1):
        ctrl._poll_user()
        assert ctrl.pending is not None
    ctrl._poll_user()
    assert ctrl.pending is None


def test_flickering_light_commits_at_poll_cap(sp, ctrl):
    start_pending(ctrl)
    comps = (2.0, 4.0)
    for i in range(sp.CTRL_SETTLE_MAX_POLLS - 1):
        ctrl.on_light(comps[i % 2])                 # never settles
        ctrl._poll_user()
        assert ctrl.pending is not None
    ctrl.on_light(2.0)
    ctrl._poll_user()
    assert ctrl.pending is None                # cap reached


def test_second_move_mid_pending_restarts(sp, ctrl):
    start_pending(ctrl)
    ctrl._poll_user()
    ctrl._poll_user()
    ctrl.sysfs["brightness"] = 1000                 # user changes their mind
    ctrl._poll_user()
    assert ctrl.pending["frac"] == pytest.approx(1000 / BL_MAX)
    assert ctrl.target == pytest.approx(1000 / BL_MAX)
    assert ctrl.pending["stable"] == 0 and ctrl.pending["polls"] == 0


def test_blank_mid_pending_commits_recorded_value(sp, ctrl):
    frac = start_pending(ctrl)
    ctrl.sysfs["brightness"] = 0                    # DPMS/lock mid-pending
    for _ in range(sp.CTRL_SETTLE_POLLS):
        ctrl._poll_user()
    assert ctrl.pending is None
    assert any(f == pytest.approx(frac) for _, f in ctrl.curve)


def test_own_write_is_not_adopted(ctrl):
    ctrl.curve = [[1.0, 0.25]]
    ctrl.on_light(4.0)
    ctrl.last_written = 2082
    ctrl.sysfs["brightness"] = 2082      # the resync watcher's echo, or us
    ctrl._poll_user()
    assert ctrl.pending is None
    assert ctrl.curve == [[1.0, 0.25]]


def ramp(ctrl, ticks):
    """Drive _tick with the fake sysfs following each write."""
    for _ in range(ticks):
        ctrl._tick()
        if ctrl.last_written is not None:
            ctrl.sysfs["brightness"] = ctrl.last_written


def test_mid_ramp_echo_of_recent_write_not_adopted(ctrl):
    # the watcher syncs mid-ramp; mutter echoes a value we wrote up to a
    # second ago back into sysfs — it must be recognized as our own
    ctrl.on_light(4.0)
    ctrl.on_light(20.0)                  # big gap: ramp starts
    ramp(ctrl, 8)
    echo = ctrl.recent_writes[0][1]      # oldest write still in the ring
    ctrl.sysfs["brightness"] = echo
    ctrl._poll_user()
    assert ctrl.pending is None          # not read as user input
    ctrl._tick()
    assert ctrl.last_written != echo     # re-adopted and kept ramping


def test_stale_echo_beyond_ring_is_adopted(sp, ctrl):
    ctrl.on_light(4.0)
    ctrl.on_light(20.0)
    ramp(ctrl, 3)
    stale = ctrl.recent_writes[0][1]
    ramp(ctrl, sp.CTRL_ECHO_TICKS + 1)          # ring expires
    ctrl.sysfs["brightness"] = stale     # too old to be our echo
    ctrl._poll_user()
    assert ctrl.pending is not None      # treated as a real user write


def test_zero_write_is_screen_blanking_not_intent(ctrl):
    ctrl.curve = [[1.0, 0.25]]
    ctrl.on_light(4.0)
    ctrl.last_written = 2082
    ctrl.sysfs["brightness"] = 0         # DPMS/lock
    ctrl._poll_user()
    assert ctrl.pending is None
    assert ctrl.curve == [[1.0, 0.25]]


def test_write_within_deadband_of_target_ignored(ctrl):
    ctrl.curve = [[1.0, 0.25]]
    ctrl.on_light(4.0)                   # target = 1.0
    ctrl.target = 2050 / BL_MAX
    ctrl.last_written = 2082
    ctrl.sysfs["brightness"] = 2050      # mid-ramp wobble, not a decision
    ctrl._poll_user()
    assert ctrl.pending is None


# --- state: load/save/migration; corrupt files must never poison us ---

def load_curve(sp, tmp_path, content):
    c = sp.BacklightController.__new__(sp.BacklightController)
    c.curve = None
    c.state_dir = str(tmp_path)     # legacy migration re-saves on load
    c.state_file = str(tmp_path / "anchor.json")
    if content is not None:
        (tmp_path / "anchor.json").write_text(content)
    c._load_state()
    return c.curve


def test_load_state_migrates_legacy_anchor(sp, tmp_path):
    assert load_curve(sp, tmp_path, json.dumps({"k": 0.5})) == [[1.0, 0.5]]


def test_load_state_clamps_oversized_legacy_anchor(sp, tmp_path):
    # k > 1 already saturated the old law; migrate to the saturated point
    assert load_curve(sp, tmp_path, json.dumps({"k": 5.0})) == [[1.0, 1.0]]


def test_load_state_valid_curve(sp, tmp_path):
    pts = [[1.0, 0.1], [4.0, 0.5], [20.0, 1.0]]
    assert load_curve(sp, tmp_path, json.dumps({"curve": pts})) == pts


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
    '{"k": true}',               # bool is an int in Python; reject anyway
    '{"curve": "x"}',            # curve wrong type
    '{"curve": []}',             # empty
    '{"curve": [[1.0]]}',        # wrong point shape
    '{"curve": [[1.0, 0.5], [1.0, 0.6]]}',    # not strictly increasing
    '{"curve": [[0.5, 0.5]]}',   # comp below the floor
    '{"curve": [[1.0, 0]]}',     # frac zero
    '{"curve": [[1.0, 1.5]]}',   # frac above 1
    '{"curve": [[1.0, NaN]]}',   # NaN frac
    '{"curve": %s}' % json.dumps([[float(i + 1), 1.0] for i in range(65)]),
])
def test_load_state_rejects_corrupt(sp, tmp_path, content):
    assert load_curve(sp, tmp_path, content) is None


def test_state_roundtrip(sp, tmp_path):
    c = sp.BacklightController.__new__(sp.BacklightController)
    c.curve = [[1.0, 0.1234], [4.0, 0.5]]
    c.state_dir = str(tmp_path)
    c.state_file = str(tmp_path / "anchor.json")
    c._save_state()
    c2 = sp.BacklightController.__new__(sp.BacklightController)
    c2.curve = None
    c2.state_file = c.state_file
    c2._load_state()
    assert c2.curve == c.curve          # JSON floats roundtrip exactly
    assert not any(math.isnan(v) for p in c2.curve for v in p)
