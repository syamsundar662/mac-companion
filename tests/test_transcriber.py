"""One-pass Whisper (voice.Transcriber, one_pass_stt): no retries at higher temperatures, one encoder run per clip.
Whisper is faked: each decode it would try costs DECODE_S, like the real one on an unclear clip (6 decodes, 4.3 s)."""
import sys
import threading
import time
import types

import mlx.core as mx
import numpy as np
import pytest

from voice import SR, EncodeOnce, Transcriber

DECODE_S = 0.1


@pytest.fixture
def whisper(monkeypatch):
    """A fake mlx_whisper: an unclear clip makes it try every temperature it is given."""
    calls = []

    def transcribe(audio, temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0), **kw):
        tried = [temperature] if isinstance(temperature, float) else list(temperature)
        calls.append(tried)
        time.sleep(DECODE_S * len(tried))
        return {"text": "open spotify"}
    fake = types.ModuleType("mlx_whisper")
    fake.transcribe = transcribe
    fake.transcribe_mod = types.ModuleType("mlx_whisper.transcribe")
    fake.transcribe_mod.ModelHolder = types.SimpleNamespace(model=None, model_path=None)
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", fake.transcribe_mod)
    return calls


def timed(t):
    done, out = threading.Event(), {}
    t0 = time.monotonic()
    t.transcribe(np.zeros(SR, np.float32), lambda text, err: (out.update(text=text, s=time.monotonic() - t0), done.set()))
    assert done.wait(5)
    return out


@pytest.mark.parametrize("one_pass, decodes", [(True, 1), (False, 6)])
def test_an_unclear_clip_is_decoded_once(whisper, one_pass, decodes):
    out = timed(Transcriber("big", "small", "en", one_pass=one_pass))
    assert out["text"] == "open spotify" and len(whisper[-1]) == decodes
    if one_pass:
        assert out["s"] < 2 * DECODE_S          # was 6 decodes: 4.3 s on the real model instead of 0.9 s
    else:
        assert out["s"] >= 6 * DECODE_S


def test_the_encoder_runs_once_for_the_same_clip():
    runs = []
    enc = EncodeOnce(lambda mel: runs.append(1) or mel * 2)
    mel = mx.ones((1, 30, 8), mx.float16)
    a, b = enc(mel), enc(mx.ones((1, 30, 8), mx.float16))  # language picking, then transcribing
    assert len(runs) == 1 and mx.array_equal(a, b)
    enc(mx.zeros((1, 30, 8), mx.float16))                   # another clip: encoded again
    assert len(runs) == 2


def run(t, **kw):
    """Queue a job; returns (done event, result dict)."""
    done, out = threading.Event(), {}
    t.transcribe(np.zeros(SR, np.float32), lambda text, err: (out.update(text=text, err=err), done.set()), **kw)
    return done, out


def test_a_stale_job_still_in_line_is_skipped(whisper):
    """early_final: you spoke again before the early job started, so the final one doesn't wait behind it."""
    t = Transcriber("big", "small", "en")
    first, _ = run(t)
    stale, gone = run(t, stale=lambda: True)
    final, out = run(t)
    assert final.wait(5) and stale.is_set() and gone == {"text": "", "err": None}
    assert len(whisper) == 2 and out["text"] == "open spotify"


def test_a_running_stale_job_stops_between_tokens(whisper):
    """The early job is already decoding when you speak again: it stops at the next token (was: 1 s more wait)."""
    holder, steps = sys.modules["mlx_whisper.transcribe"].ModelHolder, []
    holder.model = types.SimpleNamespace(decoder=lambda: steps.append(1) or time.sleep(0.02))

    def transcribe(audio, **kw):
        for _ in range(50):  # 50 tokens, 1 s
            holder.model.decoder()
        return {"text": "open spotify"}
    sys.modules["mlx_whisper"].transcribe = transcribe
    t = Transcriber("big", "small", "en")
    t0 = time.monotonic()
    assert run(t)[0].wait(5) and len(steps) == 50  # loads the model
    alone = time.monotonic() - t0
    spoke = threading.Event()
    early, gone = run(t, stale=spoke.is_set)
    time.sleep(0.1)
    spoke.set()
    t0 = time.monotonic()
    final, out = run(t)
    assert final.wait(5) and gone == {"text": "", "err": None} and out["text"] == "open spotify"
    assert time.monotonic() - t0 < alone + 0.3        # the final's own 1 s, not 2 s
    assert len(steps) < 50 + 10 + 50                   # the early job stopped after about 5 tokens
    assert holder.model.decoder.stale is None          # the next job on this model isn't stoppable by it
