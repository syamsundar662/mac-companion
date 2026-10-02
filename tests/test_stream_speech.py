"""Speaking Claude's reply sentence by sentence while it is written (brain.ReplyStream, Controller.on_reply_*), with a
fake speaker: nothing is said."""
import asyncio
import os
from types import SimpleNamespace

import pytest

import brain
import companion
from brain import ReplyStream
from companion import Controller
from speech_chunks import SentenceStreamer


class FakeUI:
    def __init__(self):
        self.calls = []

    def reply_delta(self, text):
        self.calls.append(("delta", text))

    def reply_end(self):
        self.calls.append("end")

    def reply_discard(self):
        self.calls.append("discard")


def ev(kind, parent=None, **fields):
    return SimpleNamespace(event={"type": kind, **fields}, parent_tool_use_id=parent)


def text_start():
    return ev("content_block_start", content_block={"type": "text", "text": ""})


def delta(text, parent=None):
    return ev("content_block_delta", parent, delta={"type": "text_delta", "text": text})


def stop_reason(why):
    return ev("message_delta", delta={"stop_reason": why})


def test_narration_before_a_tool_is_discarded_and_blocks_stay_apart():
    ui = FakeUI()
    tap = ReplyStream(ui)
    for e in [ev("message_start"), text_start(), delta("Let me look."), ev("content_block_stop"),
              ev("content_block_start", content_block={"type": "tool_use", "name": "screenshot"}),
              ev("content_block_delta", delta={"type": "input_json_delta", "partial_json": "{}"}),
              ev("content_block_stop"), stop_reason("tool_use"), ev("message_stop"),
              text_start(), delta("It is sunny."), ev("content_block_stop"), stop_reason("end_turn")]:
        tap.feed(e)
    assert ui.calls == [("delta", "Let me look."), "discard", "discard",
                        ("delta", "\n"), ("delta", "It is sunny."), "end"]


def test_a_subagents_stream_is_ignored():
    ui = FakeUI()
    tap = ReplyStream(ui)
    tap.feed(delta("inner text", parent="toolu_1"))
    tap.feed(ev("message_delta", "toolu_1", delta={"stop_reason": "end_turn"}))
    assert ui.calls == []


class FakeSpeaker:
    def __init__(self, dropped=None):
        self.queued, self.said, self.dropped = [], [], dropped

    def enqueue(self, text, then=None, voice=None, t_heard=None):
        self.queued.append((text, voice, t_heard, then is not None))

    def say(self, text, then=None, voice=None, t_heard=None):
        self.said.append((text, voice, t_heard, then is not None))

    def drop_pending(self):
        self.queued.clear()
        return self.dropped

    def stop(self):
        pass


@pytest.fixture
def mac(monkeypatch):
    """A Controller with only what the reply's speech touches; no AppKit, nothing said, nothing in the real log."""
    monkeypatch.setattr(companion, "log", lambda msg: None)
    monkeypatch.setattr(companion, "AppHelper", SimpleNamespace(callLater=lambda d, fn, *a: fn(*a)))

    def make(**over):
        c = object.__new__(Controller)
        vars(c).update(cfg=dict(companion.DEFAULTS), speaker=FakeSpeaker(), stopping=False, had_approval=False,
                       request_lang="en", streamer=SentenceStreamer(), spoke_stream=False, stream_chars=0,
                       stream_t=10.0, t_asked=(10.0, 10.6), t_submit=11.0, approval_id=None, state="working",
                       voice_turn=True, pending_request=None, hands_active=False, stop_note="Stopped",
                       listener=SimpleNamespace(active=False), bubble=SimpleNamespace(orderOut_=lambda x: None),
                       js=lambda *a: None, bubble_say=lambda *a, **k: None)
        c.set_state = lambda s, text=None: setattr(c, "state", s)
        vars(c).update(over)
        return c
    return make


def test_sentences_are_spoken_as_they_arrive_and_only_the_first_is_timed(mac):
    c = mac()
    c.on_reply_delta("Chennai is on the coast. It is ")
    c.on_reply_delta("hot most of the year. Bye")
    c.on_reply_end()
    assert c.speaker.queued == [("Chennai is on the coast.", None, 10.0, False),
                                ("It is hot most of the year.", None, None, False), ("Bye", None, None, False)]
    assert c.spoke_stream


def test_tamil_sentences_get_the_tamil_voice(mac):
    c = mac(request_lang="ta")
    c.on_reply_delta("சென்னை ஒரு பெரிய நகரம். ")
    assert c.speaker.queued == [("சென்னை ஒரு பெரிய நகரம்.", "Vani", 10.0, False)]


def test_no_first_speech_time_after_an_approval(mac):
    c = mac(had_approval=True)
    c.on_reply_delta("Deleted the old chat. ")
    assert c.speaker.queued == [("Deleted the old chat.", None, None, False)]


def test_narration_is_dropped_and_the_first_speech_time_moves_on(mac):
    c = mac()
    c.speaker.dropped = 10.0  # the narration hadn't started: its time comes back
    c.on_reply_delta("Let me check that for you. Open")
    c.on_reply_discard()
    assert c.speaker.queued == [] and c.streamer.flush() == []  # "Open" is gone too
    c.on_reply_delta("\nIt is 31 degrees. ")
    assert c.speaker.queued == [("It is 31 degrees.", None, 10.0, False)]


def test_a_narration_already_playing_keeps_its_time(mac):
    c = mac()
    c.on_reply_delta("Let me check that for you. ")
    c.on_reply_discard()  # it was already being said: nothing comes back
    c.on_reply_delta("It is 31 degrees. ")
    assert c.speaker.queued == [("It is 31 degrees.", None, None, False)]


def test_the_spoken_length_is_capped_per_turn(mac):
    c = mac()
    for _ in range(20):
        c.on_reply_delta("This sentence is exactly fifty characters long ok. ")
    c.on_reply_end()
    assert [len(text) for text, *_ in c.speaker.queued] == [50] * 12  # 600 characters, then no more


@pytest.mark.parametrize("flag", ["stream_speech", "speak_replies"])
def test_flags_off_mean_no_streamed_speech(mac, flag):
    c = mac()
    c.cfg[flag] = False
    c.on_reply_delta("Chennai is on the coast. ")
    c.on_reply_end()
    assert c.speaker.queued == [] and not c.spoke_stream


def test_nothing_is_streamed_while_stopping(mac):
    c = mac(stopping=True)
    c.on_reply_delta("Chennai is on the coast. ")
    assert c.speaker.queued == []


def test_a_streamed_reply_ends_with_a_marker_not_a_second_say(mac):
    c = mac()
    c.on_reply_delta("Chennai is on the coast. It is hot")
    c.on_turn_done("Chennai is on the coast. It is hot", None, False)
    assert c.speaker.said == []
    assert c.speaker.queued[1:] == [("It is hot", None, None, False), ("", None, None, True)]  # then: follow-up


def test_a_streamed_reply_without_follow_up_queues_no_marker(mac):
    c = mac(voice_turn=False)
    c.on_reply_delta("Chennai is on the coast. ")
    c.on_turn_done("Chennai is on the coast.", None, False)
    assert c.speaker.queued == [("Chennai is on the coast.", None, 10.0, False)] and c.speaker.said == []


def test_a_reply_that_was_not_streamed_is_said_whole(mac):
    c = mac()
    c.on_turn_done("Opened Spotify.", None, False)
    assert c.speaker.said == [("Opened Spotify.", None, 10.0, True)]
    c = mac(had_approval=True)
    c.on_turn_done("Deleted it.", None, False)
    assert c.speaker.said == [("Deleted it.", None, None, True)]  # its time would include your answer


def test_submit_starts_a_fresh_stream(mac, monkeypatch):
    monkeypatch.setattr(companion, "NSWorkspace",
                        SimpleNamespace(sharedWorkspace=lambda: SimpleNamespace(frontmostApplication=lambda: None)))
    c = mac(state="idle", spoke_stream=True, stream_chars=500, stream_t=None, prev_app=None, last_activity=None,
            brain=SimpleNamespace(busy=False, send=lambda *a: None))
    c.streamer.feed("leftover")
    Controller.submit(c, "tell me about Chennai", lang="en", t_heard=(20.0, 20.6))
    assert (c.spoke_stream, c.stream_chars, c.stream_t, c.streamer.flush()) == (False, 0, 20.0, [])


def test_the_brain_asks_for_partial_messages(monkeypatch):
    b = brain.Brain.__new__(brain.Brain)
    b.cfg = {"name": "Mac", "user_name": "Alex"}
    assert b._options().include_partial_messages
    b.cfg["stream_speech"] = False
    assert not b._options().include_partial_messages


def test_a_turn_passes_the_stream_to_the_ui(monkeypatch):
    monkeypatch.setattr(brain, "log", lambda msg: None)
    events = [brain.StreamEvent("u1", "s", {"type": "content_block_start", "content_block": {"type": "text"}}),
              brain.StreamEvent("u2", "s", {"type": "content_block_delta",
                                            "delta": {"type": "text_delta", "text": "Hi there. "}}),
              brain.StreamEvent("u3", "s", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
              brain.ResultMessage("success", 1, 1, False, 1, "s", result="Hi there.")]

    class Client:
        async def query(self, prompt):
            pass

        async def receive_response(self):
            for e in events:
                yield e

    ui = FakeUI()
    ui.turn_done = lambda final, error, interrupted: ui.calls.append(("done", final, error))
    b = brain.Brain.__new__(brain.Brain)
    vars(b).update(cfg={"name": "Mac", "user_name": "Alex"}, ui=ui, router=None, client=Client(), local_notes=[],
                   turns_since_fresh=0, busy=False, _interrupted=False)

    async def run():
        b.lock = asyncio.Lock()
        await b._turn("hi", "")
    asyncio.run(run())
    assert ui.calls == [("delta", "Hi there. "), "end", ("done", "Hi there.", None)]


def test_hands_come_from_the_repo_with_the_configured_kill_switch(monkeypatch):
    monkeypatch.delenv("MAC_COMPUTER_MIC_GUARD", raising=False)
    b = brain.Brain.__new__(brain.Brain)
    b.cfg = {"name": "Mac", "user_name": "", "kill_switch_file": "~/stop-here", "step_pause": 0.2}
    mac = b._options().mcp_servers["mac"]
    assert mac["args"] == [str(brain.HERE / "hands/server.py")]
    assert mac["env"] == {"MAC_COMPUTER_PAUSE": "0.2", "MAC_COMPUTER_STOP": os.path.expanduser("~/stop-here")}
    b.cfg = {"name": "Mac", "user_name": "", "cliclick": "~/bin/cliclick"}
    assert b.stop_file == brain.Path(os.path.expanduser("~/.mac-companion/STOP"))
    assert b._mac_env()["MAC_COMPUTER_CLICLICK"] == os.path.expanduser("~/bin/cliclick")
