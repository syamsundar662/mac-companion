"""The voiceprint learns from hold-to-talk requests (voiceid.VoiceID.learn / adapt, Controller.on_heard).
Synthetic vectors and temp paths only: the real voiceprint.npy is never read or written."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import companion
import voiceid
from companion import Controller
from early_final import EarlyFinal
from voiceid import VoiceID


def unit(*v):
    v = np.array(v, dtype=np.float32)
    return v / np.linalg.norm(v)


@pytest.fixture
def vid(tmp_path):
    path = tmp_path / "voiceprint.npy"
    np.save(path, unit(1, 0, 0))
    return VoiceID(path=path)


def test_moves_towards_the_new_sample_and_stays_unit_length(vid):
    sample = unit(0.8, 0.6, 0)
    before = float(vid.profile @ sample)
    sim, updated = vid.learn(sample)
    assert updated and sim == pytest.approx(0.8)
    assert float(vid.profile @ sample) > before
    assert np.linalg.norm(vid.profile) == pytest.approx(1.0)
    np.testing.assert_allclose(np.load(vid.path), vid.profile)  # saved


def test_ignores_low_score_samples(vid):
    sim, updated = vid.learn(unit(0.5, 0.866, 0))
    assert not updated and sim == pytest.approx(0.5, abs=1e-3)
    np.testing.assert_allclose(vid.profile, unit(1, 0, 0))
    np.testing.assert_allclose(np.load(vid.path), unit(1, 0, 0))


def test_adapt_embeds_the_audio(vid, monkeypatch):
    monkeypatch.setattr(vid, "_embed", lambda audio: unit(0.9, 0.436, 0))
    assert vid.adapt(np.zeros(16000, np.float32))[1]
    assert VoiceID(path=vid.path).profile[1] > 0  # a fresh load sees the update


def test_not_enrolled_learns_nothing(tmp_path):
    v = VoiceID(path=tmp_path / "none.npy")
    assert v.adapt(np.zeros(16000, np.float32)) == (None, False) and not v.path.exists()


def test_save_is_atomic_and_keeps_a_symlink(tmp_path):
    real = tmp_path / "real.npy"
    np.save(real, unit(1, 0, 0))
    link = tmp_path / "link.npy"
    link.symlink_to(real)
    v = VoiceID(path=link)
    v.learn(unit(1, 0.1, 0))
    assert link.is_symlink() and np.load(real)[1] > 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["link.npy", "real.npy"]  # no temp file left


def test_the_default_path_is_the_real_voiceprint():
    assert voiceid.PROFILE.name == "voiceprint.npy"  # so every other test here must pass a temp path


@pytest.fixture
def mac(monkeypatch):
    """A Controller with only what on_heard touches; no AppKit, no Whisper."""
    monkeypatch.setattr(companion, "log", lambda msg: None)
    learned = threading.Event()

    def make(**over):
        c = object.__new__(Controller)
        vars(c).update(cfg=dict(companion.DEFAULTS), state="listening", preview_gen=0, enroll=None, following=False,
                       held=False, approval_id=None, t_heard=None, early=EarlyFinal(),
                       listener=SimpleNamespace(voiced=40), voiceid=SimpleNamespace(enrolled=True),
                       transcriber=SimpleNamespace(transcribe=lambda *a, **k: None))
        c.set_state = lambda s, text=None: setattr(c, "state", s)
        c.whisper_context = lambda: ""
        c.learn_voice = lambda audio: learned.set()
        c.learned = learned
        vars(c).update(over)
        return c
    return make


@pytest.mark.parametrize("over, learns", [
    ({"held": True}, True),
    ({"held": False}, False),  # wake word or hotkey: not certainly you
    ({"held": True, "voiceid": SimpleNamespace(enrolled=False)}, False),
])
def test_on_heard_learns_only_from_hold_to_talk(mac, over, learns):
    c = mac(**over)
    c.on_heard(np.zeros(16000, np.float32), None, 10.0, 10.6)
    assert c.learned.wait(1 if learns else 0.1) == learns and c.state == "transcribing"


def test_the_flag_turns_it_off(mac):
    c = mac(held=True)
    c.cfg["voice_adapt"] = False
    c.on_heard(np.zeros(16000, np.float32), None, 10.0, 10.6)
    assert not c.learned.wait(0.1)
