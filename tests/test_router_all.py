import asyncio
import re
from types import SimpleNamespace

import pytest

import brain
import router
from router import Router, dark_mode_script, volume_level, why_claude


def answer(choice, confidence=0.97):
    """Shaped like a Jev Choice answer: what route() reads."""
    return SimpleNamespace(choice=choice, confidence=confidence, probabilities={choice: confidence})


def answers(intent, confidence=0.97, app="none", single=0.99):
    return {"intent": answer(intent, confidence), "app": answer(app), "single": answer("single", single)}


@pytest.fixture
def jev(monkeypatch):
    """A Router whose Jev is faked and whose actions are only recorded, never done.
    Set jev.answers to what Jev should say (jev.delay to make it slow, jev.error to make it fail); jev.asked lists
    what Jev was asked, jev.done what route() would have done."""
    monkeypatch.setattr(router, "load_key", lambda: True)
    done, asked, logs = [], [], []
    monkeypatch.setattr(router, "do", lambda intent, text, app: done.append((intent, text, app)) or "Done.")

    async def fake_ask(self, text, reply_to=None):
        asked.append(text)
        await asyncio.sleep(jev.delay)
        if jev.error:
            raise jev.error
        return jev.answers
    monkeypatch.setattr(Router, "_ask", fake_ask)
    jev = SimpleNamespace(router=Router(logs.append), answers=None, delay=0, error=None, asked=asked, done=done,
                          logs=logs)
    return jev


def timed(line):
    return re.search(r", \d+\.\d\ds$", line)


def test_long_request_reaches_jev(jev):
    text = "can you please open Safari and search for the weather in Chennai for tomorrow morning"
    assert len(text.split()) == 15
    jev.answers = answers("other", 1.0)
    assert asyncio.run(jev.router.route(text)) is None
    assert jev.asked == [text] and jev.done == []
    assert jev.logs[-1].startswith("fast path: other, ") and timed(jev.logs[-1])
    assert "Safari" not in " ".join(jev.logs)  # the log never holds what was said


def test_simple_request_done_with_timing(jev):
    jev.answers = answers("volume_up")
    assert asyncio.run(jev.router.route("volume up")) == ("volume_up", "Done.", True)
    assert jev.done == [("volume_up", "volume up", "none")]
    assert jev.logs[-1].startswith("fast path: volume_up (0.97), ") and timed(jev.logs[-1])


@pytest.mark.parametrize("said", [answers("open_app", app="Spotify", single=0.1),  # "open Spotify and play some lofi"
                                  answers("volume_up", confidence=0.6),
                                  answers("open_app", app="none")])
def test_not_one_sure_simple_action_goes_to_claude(jev, said):
    jev.answers = said
    assert asyncio.run(jev.router.route("anything")) is None
    assert jev.done == []


def test_app_answer_is_logged(jev):
    jev.answers = answers("open_app", app="none")
    asyncio.run(jev.router.route("open Spotify"))
    assert jev.logs[-1].startswith("fast path: app none 0.97, ")


@pytest.mark.parametrize("said", [{}, {k: v for k, v in answers("volume_up").items() if k != "single"},
                                  {k: v for k, v in answers("volume_up").items() if k != "intent"}])
def test_incomplete_answer_goes_to_claude(jev, said):
    jev.answers = said
    assert asyncio.run(jev.router.route("volume up")) is None
    assert jev.done == []
    assert jev.logs[-1].startswith("fast path: incomplete answer, ")
    assert why_claude(said) == "incomplete answer"


def test_reply_without_kind_is_kept(jev):
    jev.answers = answers("volume_up")  # no "kind": treat it as meant for Mac, never drop it
    assert asyncio.run(jev.router.route("volume up", reply_to="Done.")) == ("volume_up", "Done.", True)


def test_background_talk_ignored(jev):
    jev.answers = answers("other") | {"kind": answer("background", 0.9)}
    assert asyncio.run(jev.router.route("and then he said", reply_to="Done.")) == ("ignore", None)
    assert jev.logs[-1].startswith("fast path: ignored background talk (0.00), ") and timed(jev.logs[-1])


class TypeSafeAuthenticationError(Exception):
    pass  # route() knows it by name


@pytest.mark.parametrize("error, line", [(None, "fast path: Jev too slow, using Claude, "),
                                          (RuntimeError("boom"), "fast path: RuntimeError, using Claude, "),
                                          (TypeSafeAuthenticationError(), "fast path: the Typesafe API key was rejected")])
def test_jev_failures_go_to_claude(jev, monkeypatch, error, line):
    jev.answers = answers("volume_up")
    if error is None:
        monkeypatch.setattr(router, "TIMEOUT_S", 0.01)
        jev.delay = 1
    jev.error = error
    assert asyncio.run(jev.router.route("volume up")) is None
    assert jev.done == []
    assert jev.logs[-1].startswith(line) and timed(jev.logs[-1])
    assert jev.router.enabled is not isinstance(error, TypeSafeAuthenticationError)  # a rejected key turns it off


@pytest.mark.parametrize("fake_do, line", [(lambda *a: 1 / 0, "fast path: set_volume failed (ZeroDivisionError"),
                                           (lambda *a: None, "fast path: set_volume needs Claude, ")])
def test_do_failing_goes_to_claude(jev, monkeypatch, fake_do, line):
    monkeypatch.setattr(router, "do", fake_do)
    jev.answers = answers("set_volume")
    assert asyncio.run(jev.router.route("set the volume")) is None
    assert jev.logs[-1].startswith(line) and timed(jev.logs[-1])


def test_route_crash_still_reaches_claude(monkeypatch):
    """Whatever goes wrong in the fast path, the request still goes to Claude, and the turn ends."""
    logs, reached = [], []
    monkeypatch.setattr(brain, "log", logs.append)  # not the real companion.log

    async def crash(text, reply_to=None):
        raise KeyError("intent")

    async def connect(quiet=False):
        reached.append("claude")  # stays unconnected, so the turn ends with "Claude isn't connected"
    b = brain.Brain.__new__(brain.Brain)
    b.cfg, b.client, b.last_reply, b.local_notes = {"user_name": "Alex"}, None, "", []
    b.router, b._connect = SimpleNamespace(route=crash), connect
    b.ui = SimpleNamespace(turn_done=lambda *a: reached.append(a[1]))

    async def turn():
        b.lock = asyncio.Lock()
        await b._turn("volume up", None)
    asyncio.run(turn())
    assert reached == ["claude", "Claude isn't connected. Check companion.log."]
    assert len(logs) == 1 and logs[0].startswith("fast path: KeyError, using Claude, ") and timed(logs[0])


@pytest.mark.parametrize("text, level", [("set volume to 30", 30), ("set the volume to 5", 5), ("volume 30%", 30),
                                         ("half volume", 50), ("max volume", 100), ("set volume to 150", 100),
                                         ("set the volume", None),
                                         ("it is 2 am, set the volume to 20", None),  # a reason with a number
                                         ("I have a call in 5 minutes, set volume to 40", None),
                                         ("it's 2 am, half volume", None)])
def test_volume_level(text, level):
    assert volume_level(text) == level


def test_unclear_level_goes_to_claude_untouched():
    assert router.do("set_volume", "it is 2 am, set the volume to 20", "none") is None  # conftest: no osascript call


def test_dark_and_light_mode_come_from_the_intent():
    """Jev picks the direction, so a reason like "the lights are off" can't flip it."""
    assert dark_mode_script("dark_mode").endswith("set dark mode to true")
    assert dark_mode_script("light_mode").endswith("set dark mode to false")
    assert {"dark_mode", "light_mode"} <= router.INTENTS.keys() and {"dark_mode", "light_mode"} <= router.SILENT
