"""Replying faster: a quick model for plain conversation (chat_model), Jev asked during the end-of-speech wait
(early_jev) and no cold starts (keep_warm). Jev and Claude are faked."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import brain
import companion
from early_final import EarlyFinal
from tests.test_early_final import FakeTranscriber, mac  # noqa: F401 (mac is a fixture)
from tests.test_router_all import answer, answers, jev  # noqa: F401 (jev is a fixture)


def talking(p, intent="other"):
    return answers(intent, 1.0) | {"talk": answer("talk", p)}


@pytest.mark.parametrize("talk, chat", [(0.97, True), (0.85, False), (None, False)])
def test_jev_says_whether_its_only_conversation(jev, talk, chat):
    jev.answers = answers("other", 1.0) if talk is None else talking(talk)
    assert asyncio.run(jev.router.route("tell me a joke")) is None
    assert jev.router.chat is chat
    assert jev.logs[-1].startswith("fast path: other, chat, " if chat else "fast path: other, 0")


def test_a_command_done_instantly_is_never_a_chat(jev):
    jev.router.chat = True  # left over from the last request
    jev.answers = talking(0.1, "volume_up")
    assert asyncio.run(jev.router.route("volume up"))[0] == "volume_up" and not jev.router.chat


class Client:
    """A Claude session that records the model each turn ran on."""
    def __init__(self):
        self.model, self.turns, self.switches = "sonnet", [], 0

    async def set_model(self, model):
        self.model, self.switches = model, self.switches + 1

    async def query(self, prompt):
        self.turns.append((self.model, prompt))

    async def receive_response(self):
        yield brain.ResultMessage("success", 1, 1, False, 1, "s", result="Ok.")


def make_brain(monkeypatch, chats, **cfg):
    """chats: what Jev says about each request in turn (True: only conversation)."""
    monkeypatch.setattr(brain, "log", lambda msg: None)
    said = iter(chats)

    async def route(text, reply_to=None):
        b.router.chat = next(said)
    b = brain.Brain.__new__(brain.Brain)
    vars(b).update(cfg={"name": "Mac", "user_name": "Alex", "model": "sonnet", "chat_model": "haiku"} | cfg,
                   ui=SimpleNamespace(turn_done=lambda *a: None, assistant_text=lambda t: None),
                   router=SimpleNamespace(route=route, chat=False), client=Client(), local_notes=[],
                   turns_since_fresh=0, busy=False, _interrupted=False, last_reply="", model="sonnet", _approvals={})
    return b


def turns(b, *texts):
    async def run():
        b.lock = asyncio.Lock()
        for t in texts:
            await b._turn(t, "")
    asyncio.run(run())
    return [m for m, _ in b.client.turns]


def test_a_chat_runs_on_the_quick_model_in_the_same_session(monkeypatch):
    b = make_brain(monkeypatch, [True, True, False, True])
    assert turns(b, "tell me a joke", "another one", "open Spotify", "thanks") == ["haiku", "haiku", "sonnet", "haiku"]
    assert b.client.switches == 3  # only when it changes


@pytest.mark.parametrize("cfg", [{"chat_model": ""}, {"chat_model": None}])
def test_chat_model_off_keeps_the_normal_model(monkeypatch, cfg):
    b = make_brain(monkeypatch, [True, False], **cfg)
    assert turns(b, "tell me a joke", "open Spotify") == ["sonnet", "sonnet"] and b.client.switches == 0


def test_tamil_never_asks_jev_so_it_stays_on_the_normal_model(monkeypatch):
    b = make_brain(monkeypatch, [])

    async def run():
        b.lock = asyncio.Lock()
        await b._turn("வணக்கம்", "", lang="ta")
    asyncio.run(run())
    assert [m for m, _ in b.client.turns] == ["sonnet"]


# ---- early_jev ----

def early_then_final(jev, early, final, reply_to=None, gap=0.0):
    """Jev asked on the early text, then the request (final text) routed `gap` seconds later."""
    async def run():
        jev.router.prefetch(early)
        await asyncio.sleep(gap)
        t0 = asyncio.get_running_loop().time()
        r = await jev.router.route(final, reply_to)
        return r, asyncio.get_running_loop().time() - t0
    return asyncio.run(run())


def test_the_early_answer_is_used_when_nothing_more_was_said(jev):
    jev.answers, jev.delay = talking(0.97), 0.3
    _, waited = early_then_final(jev, "tell me a joke", "tell me a joke", gap=0.35)  # ready before listening ended
    assert jev.asked == ["tell me a joke"] and waited < 0.05 and jev.router.chat
    assert jev.logs[-1].startswith("fast path: other, chat, early, ")


@pytest.mark.parametrize("final, reply_to", [("tell me a joke about cats", None), ("tell me a joke", "Sure?")])
def test_jev_is_asked_again_when_the_final_text_differs(jev, final, reply_to):
    jev.answers = answers("other", 1.0)
    early_then_final(jev, "tell me a joke", final, reply_to)
    assert jev.asked == ["tell me a joke", final] and ", early, " not in jev.logs[-1]


def controller(monkeypatch, **over):
    """A Controller with only what on_early touches; its brain records what Jev is asked early."""
    monkeypatch.setattr(companion, "on_main", lambda fn, *a: fn(*a))
    c, asked = object.__new__(companion.Controller), []
    vars(c).update(cfg=dict(companion.DEFAULTS) | over.pop("cfg", {}), transcriber=FakeTranscriber(),
                   early=EarlyFinal(), approval_id=None, following=False, wake_names=["mac"], asked=asked,
                   brain=SimpleNamespace(prefetch=lambda *a: asked.append(a)))
    vars(c).update(over)
    c.early.start(40)
    return c


@pytest.mark.parametrize("text, want", [("Hey Mac, tell me a joke", ("tell me a joke", False, "en")),
                                        ("stop", None), ("", None)])
def test_the_early_text_goes_to_jev_as_it_would_be_sent(monkeypatch, text, want):
    c = controller(monkeypatch)
    c.on_early(40, text, None)
    assert c.asked == ([want] if want else []) and c.early.done


@pytest.mark.parametrize("over", [{"cfg": {"early_jev": False}}, {"approval_id": 3}])
def test_no_early_jev_when_off_or_answering_a_question(monkeypatch, over):
    c = controller(monkeypatch, **over)
    c.on_early(40, "tell me a joke", None)
    assert c.asked == []


# ---- keep_warm ----

@pytest.mark.parametrize("idle, calls", [(180, 1), (30, 0)])
def test_jev_is_warmed_once_after_two_quiet_minutes(jev, monkeypatch, idle, calls):
    jev.answers = answers("time")
    jev.router.last_call = time.monotonic() - idle

    async def run():
        for _ in range(3):  # listening started three times in a row
            await jev.router.warm()
    asyncio.run(run())
    assert len(jev.asked) == calls


class SlowClient(Client):
    """Answers after `delay` seconds, like Claude on a cold connection."""
    def __init__(self, delay=0.0):
        super().__init__()
        self.delay = delay

    async def receive_response(self):
        await asyncio.sleep(self.delay)
        yield brain.ResultMessage("success", 1, 1, False, 1, "s", result="ok")


def warm_brain(monkeypatch, idle_s, delay=0.0, turns=0):
    logs = []
    b = make_brain(monkeypatch, [])
    monkeypatch.setattr(brain, "log", logs.append)
    b.client, b.last_claude, b.turns_since_fresh, b.logs = SlowClient(delay), time.monotonic() - idle_s, turns, logs
    b.ui.assistant_text = b.ui.turn_done = lambda *a: pytest.fail("a warm-up reached the UI")
    b.router = None
    return b


@pytest.mark.parametrize("idle_min, turns", [(11, 1), (3, 0)])
def test_claude_is_warmed_once_after_a_quiet_spell(monkeypatch, idle_min, turns):
    b = warm_brain(monkeypatch, 60 * idle_min)

    async def run():
        b.lock = asyncio.Lock()
        for _ in range(3):
            await b._warm()
    asyncio.run(run())
    assert len(b.client.turns) == turns
    if turns:
        assert b.client.turns[0] == ("sonnet", "[Alex is about to ask you something. Reply with only: ok]")
        assert b.logs[-1].startswith("keep warm: Claude ready, ")


def test_an_old_conversation_is_made_fresh_before_the_warm_up(monkeypatch):
    b = warm_brain(monkeypatch, 3600, turns=4)
    old, fresh = b.client, SlowClient()
    b.client.disconnect = lambda: asyncio.sleep(0)

    async def connect(quiet=False):
        b.client = fresh
    b._connect = connect

    async def run():
        b.lock = asyncio.Lock()
        await b._warm()
    asyncio.run(run())
    assert (old.turns, len(fresh.turns), b.turns_since_fresh) == ([], 1, 0)


def test_a_slow_warm_up_never_holds_up_an_instant_command(monkeypatch):
    b = warm_brain(monkeypatch, 3600, delay=1.0)
    done = []

    async def route(text, reply_to=None):
        return "volume_up", "Volume 60%.", True
    b.router = SimpleNamespace(route=route, chat=False, warm=lambda: asyncio.sleep(0))
    b.ui.assistant_text = lambda t: None
    b.ui.turn_done = lambda final, *a: done.append((final, time.monotonic()))

    async def run():
        b.lock = asyncio.Lock()
        warming = asyncio.create_task(b._warm())
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await b._turn("volume up", "")
        await warming
        return done[0][1] - t0
    assert asyncio.run(run()) < 0.1 and done[0][0] == "Volume 60%."


@pytest.mark.parametrize("on", [True, False])
def test_listening_starts_the_warm_up(mac, on):
    warmed = []
    c = mac(listener=SimpleNamespace(active=False, start=lambda **k: None), speaker=SimpleNamespace(stop=lambda: None),
             brain=SimpleNamespace(warm=lambda: warmed.append(1)))
    c.cfg["keep_warm"] = on
    companion.Controller.start_listening(c)
    assert warmed == ([1] if on else [])


# ---- natural ----

@pytest.mark.parametrize("on", [True, False])
def test_mac_talks_like_a_friend(on):
    b = brain.Brain.__new__(brain.Brain)
    b.cfg = {"name": "Mac", "user_name": "Alex", "natural": on}
    p = b._system_prompt()
    assert ("like a friend talking out loud" in p and "Contractions" in p and '"Certainly"' in p) is on
    assert ("give the answer in one short sentence" in p) is not on
    assert "So you\n  can hear Alex." in p and "Never narrate." in p  # kept either way
    assert "\u2014" not in p and "\u2013" not in p


def test_no_name_means_the_user_and_one_language_means_no_language_rules():
    b = brain.Brain.__new__(brain.Brain)
    b.cfg = {"name": "Mac", "user_name": "", "languages": ["en"]}
    p = b._system_prompt()
    assert "You are Mac, the user's companion." in p and "\n- The user shares this keyboard" in p
    assert "# Languages" not in p and "Tamil" not in p
    b.cfg["languages"] = ["en", "ta", "ml"]
    assert "The user speaks English, Tamil and Malayalam." in b._system_prompt()


class StoppableClient(SlowClient):
    """A slow warm-up that a stop cuts short, the way client.interrupt() ends a turn."""
    interrupted = False

    async def interrupt(self):
        self.interrupted = True

    async def receive_response(self):
        t0 = time.monotonic()
        while time.monotonic() - t0 < self.delay and not self.interrupted:
            await asyncio.sleep(0.01)
        yield brain.ResultMessage("success", 1, 1, False, 1, "s", result="ok")


def test_a_stop_while_waiting_behind_the_warm_up_ends_the_request(monkeypatch):
    """Was: the stop was lost (nothing busy to interrupt) and the request ran once the warm-up finished."""
    b = warm_brain(monkeypatch, 3600, delay=5.0)
    b.client, done = StoppableClient(5.0), []
    b.ui.turn_done = lambda *a: done.append(a)

    async def run():
        b.lock = asyncio.Lock()
        warming = asyncio.create_task(b._warm())
        await asyncio.sleep(0.05)
        asking = asyncio.create_task(b._turn("close all my windows", ""))
        await asyncio.sleep(0.05)
        t0 = time.monotonic()
        await b._interrupt()
        await asyncio.gather(warming, asking)
        return time.monotonic() - t0
    assert asyncio.run(run()) < 0.5
    assert done == [("", None, True)] and len(b.client.turns) == 1  # only the warm-up reached Claude


def test_a_stop_for_one_request_doesnt_end_the_next(monkeypatch):
    b = make_brain(monkeypatch, [False, False])
    done = []
    b.ui.turn_done = lambda *a: done.append(a[2])

    async def run():
        b.lock = asyncio.Lock()
        await b._turn("what's on my screen", "")
        await b._interrupt()  # a late stop, after it finished
        await b._turn("and now", "")
    asyncio.run(run())
    assert done == [False, False] and len(b.client.turns) == 2


def test_the_warm_up_can_use_no_tools(monkeypatch):
    """Nothing on screen, no activity line and no safety check for the "ok" turn."""
    b = warm_brain(monkeypatch, 3600)
    b.ui.activity = lambda t: pytest.fail("a warm-up showed activity")
    b.warming = True
    out = asyncio.run(b._pre_tool({"tool_name": "mcp__mac__screenshot", "tool_input": {}}, "t1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_a_stuck_warm_up_lets_go_of_the_lock(monkeypatch):
    b = warm_brain(monkeypatch, 3600, delay=60)
    monkeypatch.setattr(brain, "WARM_TIMEOUT_S", 0.1)

    async def run():
        b.lock = asyncio.Lock()
        await b._warm()
        return b.lock.locked()
    assert not asyncio.run(run()) and b.client is None and not b.warming
    assert b.logs[-1].startswith("keep warm: Claude failed (TimeoutError)")
