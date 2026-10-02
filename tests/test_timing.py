import io
import threading
from types import SimpleNamespace

import numpy as np
import pytest

import companion
import voice
from companion import Controller, answer_timing
from tests.fakes import LiveMic
from voice import END_SILENCE_S, SR, WAKE_END_SILENCE_S, Listener, Speaker


def test_answer_timing():
    heard = (10.0, 10.7)  # (you stopped talking, listening ended)
    assert answer_timing(heard, 11.74, 13.3) == "timing: pause 0.7s, transcribe 1.0s, answer 1.6s"
    assert answer_timing(heard, 11.74, 13.3, approval=True).endswith("answer 1.6s (with approval)")
    assert answer_timing(None, 11.0, 12.0) is None  # typed: nothing was heard
    assert answer_timing(heard, None, 12.0) is None
    assert answer_timing(heard, 11.0, 12.0, error="boom") is None
    assert answer_timing(heard, 11.0, 12.0, interrupted=True) is None


def tone(seconds):
    t = np.arange(int(seconds * SR)) / SR
    return (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def hear(mic, **start):
    """Run a Listener on the mic until it hears something; returns the Listener. Fails instead of hanging."""
    done, out = threading.Event(), []

    def on_done(heard, error):
        out.append((heard, error))
        done.set()
    listener = Listener(mic, lambda level: None, on_done)
    listener.start(**start)
    assert done.wait(10), "Listener never finished"
    heard, error = out[0]
    assert heard is not None and error is None
    return listener


def test_speech_end_is_when_you_stopped_talking():
    mic = LiveMic(np.concatenate([np.zeros(SR // 2), tone(1.0)]))  # real speed: talking ends 1.5 s in
    listener = hear(mic)
    assert abs(listener.speech_end - (mic.t0 + 1.5)) < 0.15
    assert listener.listen_end - listener.speech_end > END_SILENCE_S - 0.01  # the pause it waited out


def test_speech_end_allows_for_audio_replayed_after_the_wake_word():
    # "Hey Mac, open Spotify" in one breath: the wake burst (ending in its pause) is the seed, and the second of
    # silence recorded while the wake word was checked is already queued when listening starts.
    seed = np.concatenate([tone(1.0), np.zeros(round(WAKE_END_SILENCE_S * SR), np.float32)])
    mic = LiveMic(np.zeros(SR, np.float32), backlog_s=1.0)
    listener = hear(mic, seed=seed)
    assert abs(listener.speech_end - (mic.t0 - WAKE_END_SILENCE_S)) < 0.15  # the seed ends where the mic starts


@pytest.fixture
def stub(monkeypatch):
    """A stand-in Controller with just what submit touches; no AppKit, nothing written to the real log."""
    monkeypatch.setattr(companion, "log", lambda msg: None)
    monkeypatch.setattr(companion, "NSWorkspace",
                        SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: None)))

    def make(**over):
        sent = []
        c = SimpleNamespace(stopping=False, state="idle", pending_request=None, request_lang="en", voice_turn=False,
                            t_asked=None, t_submit=None, had_approval=False, prev_app=None, last_activity=None,
                            brain=SimpleNamespace(busy=False, send=lambda *a: sent.append(a)), sent=sent,
                            js=lambda *a: None, set_state=lambda *a: None)
        vars(c).update(over)
        return c
    return make


def test_a_typed_message_dropped_as_busy_leaves_the_running_request_alone(stub):
    c = stub(state="working", voice_turn=True, t_asked=(1.0, 1.6), request_lang="ta")
    Controller.submit(c, "hello")
    assert (c.voice_turn, c.t_asked, c.request_lang, c.sent) == (True, (1.0, 1.6), "ta", [])


def test_a_request_made_while_stopping_keeps_its_language_and_times(stub):
    c = stub(stopping=True, state="working")
    Controller.submit(c, "வணக்கம்", t_heard=(1.0, 1.6))
    assert c.pending_request == ("வணக்கம்", "ta", (1.0, 1.6))
    assert (c.request_lang, c.t_asked, c.sent) == ("en", None, [])


def test_an_accepted_spoken_request_is_timed(stub):
    c = stub(had_approval=True)  # left over from the last request
    Controller.submit(c, "open Spotify", lang="en", t_heard=(1.0, 1.6))
    assert c.voice_turn and c.t_asked == (1.0, 1.6) and c.t_submit is not None and not c.had_approval
    assert c.sent == [("open Spotify", "", False, "en")]


class FakeSay:
    """Stands in for the `say` process, so the test stays silent."""
    started = []

    def __init__(self, cmd, **kw):
        self.cmd, self.stdin = cmd, io.BytesIO()
        FakeSay.started.append(self)

    def poll(self):
        return 0

    def wait(self):
        return 0


def test_first_speech_is_logged_when_say_starts(monkeypatch):
    FakeSay.started = []
    monkeypatch.setattr(voice, "subprocess", SimpleNamespace(Popen=FakeSay, PIPE=-1, DEVNULL=-3))
    monkeypatch.setattr(voice, "time", SimpleNamespace(monotonic=lambda: 13.4))
    notes, done, flushed = [], threading.Event(), threading.Event()
    s = Speaker(on_note=notes.append)
    s.say("Hi there.", then=done.set, voice="Samantha", t_heard=10.0)
    assert done.wait(1)  # spoken on the speech thread; `then` comes after the line
    assert s.started_at == 13.4 and notes == ["timing: first speech 3.4s after you stopped"]
    assert FakeSay.started[0].cmd == ["say", "-v", "Samantha"]
    s.say("Not a spoken request.")  # no t_heard: no line
    s.say("```code only```", t_heard=10.0)  # nothing speakable: nothing starts, no line
    s.enqueue("", then=flushed.set)  # runs once everything queued before it is done
    assert flushed.wait(1) and len(FakeSay.started) == 2 and len(notes) == 1
