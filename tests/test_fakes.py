import threading
import time

import numpy as np
import pytest

from tests.fakes import LiveMic
from voice import BLOCK, SR, Listener


def listen(audio, **kw):
    """Run a Listener on the clip; returns what it passed to on_done. Fails instead of hanging."""
    done, out = threading.Event(), []

    def on_done(heard, error):
        out.append((heard, error))
        done.set()
    Listener(LiveMic(audio, realtime=False, **kw), lambda level: None, on_done).start()
    assert done.wait(5), "Listener never finished"
    return out[0]


def tone(seconds, amp=0.3, hz=220):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * hz * t)).astype(np.float32)


def test_blocks_are_numbered_and_the_mic_stalls_at_the_end():
    mic = LiveMic(np.zeros(BLOCK * 3 + 10), realtime=False, tail_s=BLOCK * 2 / SR)
    q = mic.subscribe()
    deadline = time.monotonic() + 2
    while not mic.stalled and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mic.stalled
    items = [q.get_nowait() for _ in range(q.qsize())]
    assert [n for n, _ in items] == [1, 2, 3, 4, 5] and mic.count == 5
    assert all(b.dtype == np.float32 and b.shape == (BLOCK,) for _, b in items)
    assert not np.shares_memory(items[0][1], mic.audio)

    q = mic.subscribe(since=3)
    assert [q.get(timeout=2)[0] for _ in range(2)] == [4, 5]


def test_silence_ends_with_nothing_heard():
    assert listen(np.zeros(int(6.5 * SR))) == (None, None)


def test_a_loud_sound_is_heard_and_the_tail_ends_it():
    heard, error = listen(np.concatenate([np.zeros(SR // 2), tone(1.0)]))
    assert error is None and heard is not None and len(heard) >= 1.5 * SR


def test_a_clip_that_runs_out_ends_like_a_dead_mic():
    heard, error = listen(np.concatenate([np.zeros(SR // 2), tone(1.0)]), tail_s=0)
    assert heard is None and "microphone stopped" in error


def test_rejects_audio_that_is_not_mono_float():
    with pytest.raises(ValueError):
        LiveMic(np.zeros((SR, 2), np.float32))
    with pytest.raises(ValueError):
        LiveMic(np.zeros(SR, np.int16))
