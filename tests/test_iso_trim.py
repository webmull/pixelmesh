# (c) Adam Davis - adamdavis.co.uk
"""The ISO trim maths, with no remote and no camera in sight.

The trim is the part that has to be right: it decides what ISO the show
actually runs at across every detect on/off, and a mistake here is only
visible as a too-dark or blown-out crowd once you are in the room.
"""

import sys
import types
import pytest


@pytest.fixture
def ctl(monkeypatch):
    """controller with elgato stubbed out.

    Importing controller pulls in Dear PyGui and a camera; the trim helpers
    touch neither, so the module is imported with a fake elgato that just
    records what gain it was told to set.
    """
    controller = pytest.importorskip("controller")

    class FakeElgato:
        connected = True
        iso_gain  = 100
        def set_iso(self, gain):
            self.iso_gain = gain

    fake = FakeElgato()
    monkeypatch.setattr(controller, "elgato", fake)
    monkeypatch.setattr(controller, "set_status", lambda *_: None)
    controller._iso_trim = 0
    return controller, fake


def _detecting(ctl_mod, on):
    with ctl_mod.state.lock:
        ctl_mod.state.detecting = on


class TestNudge:
    def test_up_raises_by_one_step(self, ctl):
        c, fake = ctl
        _detecting(c, False)
        assert c.nudge_iso(+1) is True
        assert fake.iso_gain == c._AUDIENCE_ISO_GAIN + c._ISO_TRIM_STEP

    def test_down_lowers_by_one_step(self, ctl):
        c, fake = ctl
        _detecting(c, False)
        assert c.nudge_iso(-1) is True
        assert fake.iso_gain == c._AUDIENCE_ISO_GAIN - c._ISO_TRIM_STEP

    def test_no_camera_hub_is_a_refusal_not_a_crash(self, ctl):
        c, fake = ctl
        fake.connected = False
        assert c.nudge_iso(+1) is False
        assert c._iso_trim == 0        # and the trim does not drift either

    def test_returns_false_at_the_limit(self, ctl):
        """The remote turns a False into its 'that did nothing' buzz, so it
        has to actually report False rather than silently clamping."""
        c, _ = ctl
        _detecting(c, False)
        for _ in range(200):
            if c.nudge_iso(+1) is False:
                break
        else:
            pytest.fail("trim never reported a limit")
        assert c.nudge_iso(+1) is False


class TestStickyAcrossPhases:
    def test_trim_survives_the_detect_transition(self, ctl):
        """The whole point: a tune made mid-show is not wiped by the next
        automatic ISO move."""
        c, fake = ctl
        _detecting(c, False)
        c.nudge_iso(+1)
        c.nudge_iso(+1)
        trim = c._iso_trim
        assert trim == 2 * c._ISO_TRIM_STEP

        _detecting(c, True)
        c._apply_detection_iso()
        assert fake.iso_gain == c._DETECTION_ISO_GAIN + trim

        _detecting(c, False)
        c._apply_audience_iso()
        assert fake.iso_gain == c._AUDIENCE_ISO_GAIN + trim

    def test_zero_trim_keeps_the_old_never_raise_guard(self, ctl):
        """With no trim set, detection ISO must behave exactly as it did
        before the remote existed: lower a high gain, leave a low one."""
        c, fake = ctl
        _detecting(c, True)

        fake.iso_gain = 120
        c._apply_detection_iso()
        assert fake.iso_gain == c._DETECTION_ISO_GAIN

        fake.iso_gain = 20                      # deliberately below baseline
        c._apply_detection_iso()
        assert fake.iso_gain == 20              # left alone

    def test_reset_returns_to_the_plain_baseline(self, ctl):
        c, fake = ctl
        _detecting(c, False)
        c.nudge_iso(+1)
        c.reset_iso_trim()
        assert c._iso_trim == 0
        assert fake.iso_gain == c._AUDIENCE_ISO_GAIN


class TestSliderRebase:
    def test_slider_rebases_the_trim(self, ctl):
        """Slider and remote must never disagree about where ISO is: after
        dragging the slider, the next phase change has to honour that value
        rather than snapping back to the old trim."""
        c, fake = ctl
        _detecting(c, False)
        c.nudge_iso(-1)                          # some pre-existing trim

        c._set_iso(None, 130)
        assert fake.iso_gain == 130
        assert c._iso_trim == 130 - c._AUDIENCE_ISO_GAIN

        _detecting(c, True)
        c._apply_detection_iso()
        assert fake.iso_gain == c._DETECTION_ISO_GAIN + (130 - c._AUDIENCE_ISO_GAIN)

    def test_slider_trim_is_clamped(self, ctl):
        c, _ = ctl
        _detecting(c, False)
        c._set_iso(None, 0)
        assert c._iso_trim >= c._ISO_TRIM_MIN
