"""The final transcription started at the first short pause (early_final.py, Controller.preview_tick / on_heard), with a
fake transcriber: no Whisper, nothing said."""
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import companion
from companion import Controller
from early_final import EarlyFinal
from tests.fakes import LiveMic
from voice import SR, WAKE_END_SILENCE_S, Listener


def test_reused_only_if_no_new_speech():
    e = EarlyFinal()
    e.start(voiced=40)
    got = []
    e.result("open spotify")
    assert e.usable(final_voiced=40)
    e.when_ready(got.append)
    assert got == ["open spotify"]
    assert not e.usable(final_voiced=47)      # you kept talking: transcribe everything instead


def test_waits_for_a_running_job():
    e = EarlyFinal(); e.start(voiced=10); got = []
    e.when_ready(got.append)
    assert got == [] and e.running
    e.result("hi")
    assert got == ["hi"] and not e.running


class FakeTranscriber:
    last_language = "en"

    def __init__(self):
        self.jobs, self.previews = [], []

    def transcribe(self, audio, callback, context="", stale=None):
        self.jobs.append((len(audio), callback, context))
        self.stale = stale

    def preview(self, audio, callback):
        self.previews.append(len(audio))


@pytest.fixture
def mac(monkeypatch):
    """A Controller with only what listening touches; no AppKit, no Whisper."""
    monkeypatch.setattr(companion, "log", lambda msg: None)
    monkeypatch.setattr(companion, "on_main", lambda fn, *a: fn(*a))
    ticks = []
    monkeypatch.setattr(companion, "AppHelper", SimpleNamespace(callLater=lambda d, fn, *a: ticks.append(d)))

    def make(**over):
        c = object.__new__(Controller)
        listener = SimpleNamespace(active=True, voiced=40, quiet_for=0.3, snapshot=lambda: np.zeros(SR, np.float32))
        vars(c).update(cfg=dict(companion.DEFAULTS), listener=listener, transcriber=FakeTranscriber(),
                       early=EarlyFinal(), state="listening", preview_gen=1, preview_busy=False, preview_due=0.0,
                       enroll=None, following=False, held=False, approval_id=None, t_heard=None, ticks=ticks, heard=[],
                       wake_names=["mac"], brain=SimpleNamespace(last_active=0.0, last_reply="", prefetch=lambda *a: None,
                                                                 warm=lambda: None))
        c.set_state = lambda s, text=None: setattr(c, "state", s)
        c.on_transcribed = lambda text, err: c.heard.append((text, err))
        vars(c).update(over)
        return c
    return make


def test_the_early_final_starts_at_the_first_short_pause(mac):
    c = mac()
    c.listener.quiet_for = 0.1  # still talking (just a gap between words)
    c.preview_tick(1)
    assert c.transcriber.jobs == [] and c.transcriber.previews == [SR]
    c.listener.quiet_for = 0.3
    c.preview_tick(1)
    c.preview_tick(1)  # once per stretch of speech
    assert len(c.transcriber.jobs) == 1 and c.early.voiced == 40 and c.transcriber.previews == [SR]
    assert c.ticks == [0.1] * 3  # checked often enough to catch a 0.25 to 0.6 s pause


@pytest.mark.parametrize("quiet, voiced", [(0.6, 40), (0.2, 40), (0.3, 4)])
def test_no_early_final_outside_a_short_pause(mac, quiet, voiced):
    c = mac()
    c.listener.quiet_for, c.listener.voiced = quiet, voiced
    c.preview_tick(1)
    assert c.transcriber.jobs == []


def test_flag_off_means_the_old_way(mac):
    c = mac()
    c.cfg["early_final"] = False
    c.preview_tick(1)
    assert c.transcriber.jobs == [] and c.ticks == [0.8]


def test_new_speech_starts_a_new_one_once_the_last_is_done(mac):
    c = mac()
    c.preview_tick(1)
    c.listener.voiced = 52  # you went on talking, then paused again
    c.preview_tick(1)
    assert len(c.transcriber.jobs) == 1  # the first one is still running: a second would only queue behind it
    first = c.transcriber.jobs[0][1]
    first("open", None)
    c.preview_tick(1)
    assert len(c.transcriber.jobs) == 2 and c.early.voiced == 52 and not c.early.done
    first("open", None)  # a late answer for the old stretch changes nothing
    assert not c.early.done


def test_the_early_result_is_used_if_you_said_nothing_more(mac):
    c = mac()
    c.preview_tick(1)
    c.on_heard(np.zeros(SR, np.float32), None, 10.0, 10.6)
    assert len(c.transcriber.jobs) == 1 and c.state == "transcribing" and c.heard == []  # waits for it
    c.transcriber.jobs[0][1]("open spotify", None)
    assert c.heard == [("open spotify", None)]


def test_a_finished_early_result_is_used_at_once(mac):
    c = mac()
    c.preview_tick(1)
    c.transcriber.jobs[0][1]("open spotify", None)
    c.on_heard(np.zeros(SR, np.float32), None, 10.0, 10.6)
    assert c.heard == [("open spotify", None)] and len(c.transcriber.jobs) == 1


def test_everything_is_transcribed_if_you_kept_talking(mac):
    c = mac()
    c.preview_tick(1)
    c.listener.voiced = 47
    c.on_heard(np.zeros(2 * SR, np.float32), None, 10.0, 10.6)
    assert len(c.transcriber.jobs) == 2 and c.transcriber.jobs[1][0] == 2 * SR
    c.transcriber.jobs[0][1]("open", None)
    assert c.heard == []  # the early one is not what you said
    c.transcriber.jobs[1][1]("open spotify please", None)
    assert c.heard == [("open spotify please", None)]


def test_listening_again_forgets_the_early_result(mac):
    c = mac()
    c.preview_tick(1)
    c.transcriber.jobs[0][1]("open spotify", None)
    vars(c).update(speaker=SimpleNamespace(stop=lambda: None), state="idle")
    c.listener.active, c.listener.start = False, lambda **kw: None
    c.start_listening()
    assert c.early.voiced is None and not c.early.usable(40)


def test_a_seeded_request_can_be_snapshot():
    """ "Hey Mac, open Spotify" in one breath: the seed must be in what previews and the early final read."""
    seed = np.concatenate([np.full(SR, 0.3, np.float32), np.zeros(round(WAKE_END_SILENCE_S * SR), np.float32)])
    done = threading.Event()
    listener = Listener(LiveMic(np.zeros(SR // 2, np.float32), realtime=False), lambda level: None,
                        lambda audio, err: done.set())
    listener.start(seed=seed)
    assert done.wait(10)
    assert len(listener.snapshot()) >= len(seed)


def test_the_early_job_goes_stale_when_you_speak_again(mac):
    """Then the Transcriber drops it, so the final transcription doesn't wait behind it."""
    c = mac()
    c.preview_tick(1)
    assert not c.transcriber.stale()
    c.listener.voiced = 41
    assert c.transcriber.stale()
