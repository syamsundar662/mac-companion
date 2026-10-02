import queue
import threading
import time
from types import SimpleNamespace

import pytest

import voice
from voice import Speaker, speakable


@pytest.fixture
def says(monkeypatch):
    """Fakes `say` so nothing plays. Returns a queue of each `say` started, in order.
    A fake `say` runs until the test calls finish() (or stop() terminates it). Voice "Broken" fails to start;
    voice "Missing" quits before reading its text, like `say -v` with a voice that isn't installed."""
    started = queue.Queue()

    class FakeSay:
        def __init__(self, cmd, **kw):
            if "Broken" in cmd:
                raise OSError("no such voice")
            time.sleep(0.001)  # starting a process takes a moment
            self.cmd, self.stdin, self.text, self.rc, self.closed = cmd, self, "", None, False
            self.waiting, self.done = threading.Event(), threading.Event()
            if "Missing" in cmd:
                self.finish(1)
            started.put(self)

        def write(self, b):
            if self.rc is not None:
                raise BrokenPipeError(32, "Broken pipe")
            self.text += b.decode()

        def close(self):
            self.closed, self.t_talking = True, time.monotonic()  # it has its text: talking from here
            if self.rc is not None:
                raise BrokenPipeError(32, "Broken pipe")

        def poll(self):
            return self.rc

        def wait(self):
            self.waiting.set()
            self.done.wait(5)
            return self.rc

        def finish(self, rc=0):
            self.rc = rc
            self.done.set()

        def terminate(self):
            self.finish(-15)

    monkeypatch.setattr(voice, "subprocess", SimpleNamespace(Popen=FakeSay, PIPE=-1, DEVNULL=-3))
    return started


@pytest.fixture
def speaker(says):
    """Makes Speakers. Each is stopped at teardown, before the fake `say` is undone (this fixture needs `says`),
    so no worker is left with queued items that could reach the real `say`."""
    made = []

    def make(**kw):
        made.append(Speaker(**kw))
        return made[-1]
    yield make
    for s in made:
        s.stop()


def next_say(says):
    """The next `say` started, once the worker is waiting on it (its text, started_at and timing note are in)."""
    p = says.get(timeout=2)
    assert p.waiting.wait(2)
    return p


def drain(s):
    """Wait until everything queued so far has been handled (a marker's `then` runs when the queue reaches it)."""
    reached = threading.Event()
    s.enqueue("", then=reached.set)
    assert reached.wait(2)


def test_plays_in_order_and_calls_then(speaker, says):
    s, done = speaker(), threading.Event()
    s.enqueue("One.")
    one = next_say(says)  # already playing when Two. is queued, so they aren't joined
    s.enqueue("Two.", then=done.set)
    assert says.empty() and s.speaking  # Two. waits for One.
    one.finish()
    two = next_say(says)
    assert (one.text, two.text) == ("One.", "Two.") and not done.is_set()
    two.finish()
    assert done.wait(2)


def test_then_runs_only_after_a_clean_finish(speaker, says):
    notes, called = [], []
    s = speaker(on_note=notes.append)
    s.enqueue("Wrong voice.", then=lambda: called.append("failed"))
    next_say(says).finish(1)
    s.enqueue("Fine.", then=lambda: called.append("fine"))
    next_say(says).finish()
    drain(s)
    assert called == ["fine"] and notes == ["say exited 1"]


def test_stop_empties_the_queue_and_cancels_then(speaker, says):
    notes, called = [], []
    s = speaker(on_note=notes.append)
    s.enqueue("One.", then=lambda: called.append(1))
    one = next_say(says)
    s.enqueue("Two.", then=lambda: called.append(2))
    s.stop()
    assert one.rc == -15 and not s.speaking
    s.enqueue("Three.")  # queued after the stop: plays normally
    three = next_say(says)
    assert three.text == "Three." and called == [] and notes == []  # cut off by stop(): no "say exited" line


def test_stop_racing_the_worker_never_lets_a_stopped_item_start(speaker, says):
    s, stopped = speaker(), {}
    for i in range(100):
        s.enqueue(f"Item {i}.")
        time.sleep(i % 5 * 0.0005)  # let the stop land at different points of taking and starting the item
        s.stop()
        stopped[f"Item {i}."] = time.monotonic()
    drain(s)
    late = [p.text for p in says.queue if p.t_talking > stopped[p.text]]
    assert late == [] and all(p.rc == -15 for p in says.queue)  # what started before its stop was cut off


def test_say_cuts_off_what_is_playing_and_queued(speaker, says):
    s, called = speaker(), []
    s.enqueue("One.")
    one = next_say(says)
    s.enqueue("Two.")
    s.say("Now this.", then=lambda: called.append(1))
    assert one.rc == -15
    now = next_say(says)
    assert now.text == "Now this."
    now.finish()
    drain(s)
    assert called == [1] and says.empty()


def test_nothing_speakable(speaker, says):
    s, called = speaker(), []
    s.enqueue("One.")
    one = next_say(says)
    s.say("```only code```", then=lambda: called.append(1))  # like before: nothing happens, nothing is stopped
    s.enqueue("** **")  # empty and no `then`: ignored
    assert one.rc is None
    one.finish()
    drain(s)
    assert called == [] and says.empty()


def test_first_speech_is_logged_when_its_item_starts(speaker, says, monkeypatch):
    clock = [11.0]
    monkeypatch.setattr(voice, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    notes = []
    s = speaker(on_note=notes.append)
    s.enqueue("Narration.")  # no t_heard: no line
    one = next_say(says)
    assert s.started_at == 11.0 and notes == []
    s.enqueue("The answer.", t_heard=10.0)
    clock[0] = 12.5
    assert notes == []  # queued, not speaking yet
    one.finish()
    next_say(says)
    assert s.started_at == 12.5 and notes == ["timing: first speech 2.5s after you stopped"]


def test_waiting_items_in_the_same_voice_are_joined(speaker, says):
    notes, done = [], threading.Event()
    s = speaker(voice="Samantha", on_note=notes.append)
    s.enqueue("First.")
    first = next_say(says)
    s.enqueue("Second.", t_heard=10.0)
    s.enqueue("Third.")
    s.enqueue("Tamil.", voice="Vani")
    s.enqueue("Fourth.")
    s.enqueue("Fifth.", then=done.set)
    s.enqueue("Sixth.")  # after an item with its own `then`: not joined
    first.finish()
    heard = []
    for _ in range(4):
        p = next_say(says)
        heard.append((p.cmd[2], p.text, done.is_set()))
        p.finish()
    assert heard == [("Samantha", "Second. Third.", False), ("Vani", "Tamil.", False),
                     ("Samantha", "Fourth. Fifth.", False), ("Samantha", "Sixth.", True)]
    assert len(notes) == 1  # t_heard came from the first joined item


def test_joining_stops_before_the_600_character_cap(speaker, says):
    s = speaker()
    s.enqueue("First.")
    first = next_say(says)
    s.enqueue("a" * 400)
    s.enqueue("b" * 300)
    s.enqueue("c" * 100)
    first.finish()
    one = next_say(says)
    one.finish()
    two = next_say(says)
    assert (one.text, two.text) == ("a" * 400, "b" * 300 + " " + "c" * 100)  # nothing cut off


def test_drop_pending_keeps_the_current_say_and_hands_back_t_heard(speaker, says):
    s, done = speaker(), threading.Event()
    s.enqueue("Narration one.", then=done.set)
    one = next_say(says)
    s.enqueue("Narration two.", t_heard=10.0)
    s.enqueue("Narration three.")
    assert s.drop_pending() == 10.0
    assert one.rc is None and s.speaking  # still talking
    s.enqueue("The answer.", t_heard=10.0)
    one.finish()
    assert done.wait(2)  # the one being spoken still finishes normally
    answer = next_say(says)
    assert answer.text == "The answer."
    s.enqueue("More.")
    assert s.drop_pending() is None  # nothing dropped carried a t_heard
    assert s.drop_pending() is None  # nothing waiting


def test_marker_runs_then_when_the_queue_reaches_it(speaker, says):
    s, now, after = speaker(), threading.Event(), threading.Event()
    s.enqueue("", then=now.set)  # empty queue: runs at once
    assert now.wait(2)
    s.enqueue("One.")
    one = next_say(says)
    s.enqueue("Two.", voice="Vani")
    s.enqueue("", then=after.set)
    one.finish()
    two = next_say(says)
    assert not after.is_set()
    two.finish()
    assert after.wait(2) and says.empty()  # the marker itself starts no `say`


def test_marker_after_a_stopped_say_never_runs(speaker, says):
    s, called = speaker(), []
    s.enqueue("One.")
    one = next_say(says)
    s.enqueue("Two.")
    s.enqueue("", then=lambda: called.append(1))  # joins Two. (same voice), so it runs only if Two. finishes
    one.finish()
    two = next_say(says)
    assert two.text == "Two."
    s.stop()
    drain(s)
    assert called == []


def test_worker_survives_errors(speaker, says):
    notes = []
    s = speaker(on_note=notes.append)
    s.enqueue("", then=lambda: 1 / 0)
    s.enqueue("Hello.", voice="Broken")
    s.enqueue("Still here.")
    assert next_say(says).text == "Still here."
    assert notes == ["speech callback failed: ZeroDivisionError('division by zero')",
                     "speech failed: OSError('no such voice')"]


def test_worker_survives_a_failing_log(speaker, says):
    def on_note(msg):
        raise RuntimeError("log is gone")
    s, done = speaker(on_note=on_note), threading.Event()
    s.enqueue("", then=lambda: 1 / 0)
    s.enqueue("Hello.", voice="Broken")
    s.enqueue("Hi.", t_heard=10.0, then=done.set)  # its timing line fails too
    next_say(says).finish()
    assert done.wait(2)


def test_a_say_that_quits_before_reading_is_logged(speaker, says):
    notes, called = [], []
    s = speaker(on_note=notes.append)
    s.enqueue("Vanakkam.", voice="Missing", then=lambda: called.append(1))
    gone = next_say(says)
    s.enqueue("Next.")
    assert next_say(says).text == "Next."
    assert gone.closed and notes == ["say exited 1"] and called == []


def test_speakable_drops_an_unclosed_code_block():
    assert speakable("Run this: ```\nls -la") == "Run this:"
    assert speakable("Run ```ls``` then ```\nrm x") == "Run then"
