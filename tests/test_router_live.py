"""Live checks of what Jev decides. Skipped unless JEV_LIVE=1 (needs the TypeSafe key in .env and the network).
They only ask Jev (Router._ask), so nothing is ever opened, pressed or changed.
Run: JEV_LIVE=1 .venv/bin/python -m pytest -q -s tests/test_router_live.py"""
import asyncio
import os
import statistics
import time

import pytest

import router
from router import Router, why_claude

pytestmark = pytest.mark.skipif(os.environ.get("JEV_LIVE") != "1", reason="live Jev check, set JEV_LIVE=1")

SIMPLE = {"open Spotify": "open_app", "pause": "play_pause", "next song": "next_track", "volume up": "volume_up",
          "mute": "mute", "set volume to 30": "set_volume", "turn on dark mode": "dark_mode",
          "what time is it": "time", "what's the date": "date", "how much battery do I have": "battery",
          # one action with a reason or politeness is still one action
          "pause the music, I'm taking a call": "play_pause", "mute, the baby is sleeping": "mute",
          "open Spotify, I want to work": "open_app", "turn the volume down, it's too loud": "volume_down",
          "can you pause the music please": "play_pause", "turn on dark mode, the lights are off": "dark_mode",
          "turn off dark mode": "light_mode", "switch to light mode": "light_mode",
          # Jev should say set_volume; then do() hands these to Claude, as they hold two numbers (see volume_level)
          "it is 2 am, set the volume to 20": "set_volume", "I have a call in 5 minutes, set volume to 40": "set_volume"}
# More than one action, or details a simple action can't honour (song, website, place, person, one app only)
CLAUDE = ["open Safari and search for the weather in Chennai tomorrow", "can you open WhatsApp and send hi to Mom",
          "open Spotify and play some lofi", "what's the battery and then turn on dark mode",
          "play the next song and turn the volume up a bit", "open Notes and write down buy milk",
          "turn the volume down and open YouTube", "launch Chrome and go to gmail.com",
          "what time is it in London right now", "open Spotify and play Arijit Singh", "what time is it in Dubai",
          "set an alarm for 6 am", "open WhatsApp, I want to message Mom", "turn on dark mode in Safari only"]
# Whisper-style Tamil (ta) and Malayalam (ml) transcripts
TA_ML = {"ஸ்பாட்டிஃபை திற": "open_app", "சத்தத்தை குறை": "volume_down", "இப்போ என்ன நேரம்": "time",
         "பாட்டை நிறுத்து": "play_pause", "ശബ്ദം കൂട്ട്": "volume_up", "സമയം എത്രയായി": "time",
         "പാട്ട് നിർത്ത്": "play_pause", "டார்க் மோடை ஆஃப் பண்ணு": "light_mode", "ഡാർക്ക് മോഡ് ഓഫ് ചെയ്യ്": "light_mode"}
TA_ML_OTHER = ["அம்மாவுக்கு வாட்ஸ்அப்பில் ஹாய் அனுப்பு", "നാളത്തെ കാലാവസ്ഥ എങ്ങനെയാ", "இந்த பைலை டிலீட் பண்ணு"]


@pytest.fixture(scope="module")
def jev():
    """ask(text) -> (Jev's answers, seconds), on one kept-open connection like the app uses."""
    with asyncio.Runner() as runner:
        r = Router(lambda line: None)
        if not r.enabled:
            pytest.skip("no TYPESAFE_API_KEY")
        runner.run(r.warm_up())

        def ask(text):
            t0 = time.monotonic()
            answers = runner.run(r._ask(text))
            return answers, time.monotonic() - t0
        yield ask
        runner.run(r.close())


@pytest.fixture(autouse=True)
def nothing_is_done(monkeypatch):
    monkeypatch.setattr(router, "do", lambda *a: pytest.fail("a live check must never do anything"))


def show(text, a, took, ok):
    i, app, single = a["intent"], a["app"], a["single"].probabilities.get("single", 0)
    print(f"\n{'PASS' if ok else 'FAIL'} {text!r}: {i.choice} {i.confidence:.2f}, app {app.choice} {app.confidence:.2f}, "
          f"single {single:.2f}, {why_claude(a) or 'fast path'}, {took:.2f}s")


def fast(a, intent):
    """route() would do this itself (for opening an app: Spotify)."""
    return why_claude(a) is None and a["intent"].choice == intent and (intent != "open_app" or a["app"].choice == "Spotify")


def to_claude(a):
    """route() would send it to Claude."""
    return why_claude(a) is not None


def check(jev, cases):
    results = []
    for text, right in cases:
        a, took = jev(text)
        show(text, a, took, ok := right(a))
        results.append((text, ok, took))
    return results


def test_simple_requests_still_fast(jev):
    results = check(jev, [(t, lambda a, i=i: fast(a, i)) for t, i in SIMPLE.items()])
    assert [t for t, ok, _ in results if not ok] == []


def test_these_go_to_claude(jev):
    results = check(jev, [(t, to_claude) for t in CLAUDE])
    print(f"\nmedian Jev time over the first 5 long requests: {statistics.median(s for _, _, s in results[:5]):.2f}s")
    assert [t for t, ok, _ in results if not ok] == []


@pytest.mark.xfail(reason="'pause' is borderline (0.81 to 0.88) and Tamil 'dark mode off' comes back dark_mode, so "
                          "brain.py keeps Tamil and Malayalam off the fast path; when this passes reliably, that can go")
def test_tamil_malayalam(jev):
    results = check(jev, [(t, lambda a, i=i: fast(a, i)) for t, i in TA_ML.items()] + [(t, to_claude) for t in TA_ML_OTHER])
    assert [t for t, ok, _ in results if not ok] == []
