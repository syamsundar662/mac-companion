"""Fast path: Jev (TypeSafe, docs.typesafe.ai) looks at every request first (brain.py: English ones, for now) and
recognises simple commands in about 0.3 s, so Mac does them itself, instantly and without Claude. Anything else,
anything Jev isn't sure about, or any error goes to Claude as before. Only harmless, easy-to-undo actions live here;
quitting, deleting and sending always go through Claude. Jev also decides which of Claude's actions need your yes
(judge(), used by safety.py)."""
import asyncio
import datetime
import glob
import os
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIN_CONFIDENCE = 0.85      # below this, Claude handles it
APP_MIN_CONFIDENCE = 0.7
TIMEOUT_S = 1.0            # Jev normally answers in ~0.3 s; if it's slow, don't make you wait
FOR_ME_MIN = 0.8           # below this, what's heard after Mac spoke is gibberish or background talk (tested 10/10)
SINGLE_MIN = 0.8           # below this, it's more than one simple action (tested: one 0.99+, several 0.16 or less)
JUDGE_TIMEOUT_S = 1.5      # judge(): past this, safety.py uses its local rules
WARM_AFTER_S = 120         # keep_warm: Jev unused this long may have a cold connection (10:23: "Jev too slow")

INTENTS = {
    "open_app": "Open, launch or switch to one application by name, and nothing more (e.g. 'open Spotify', 'launch Chrome').",
    "play_pause": "Pause, resume or play whatever media is already playing, without naming a specific song, video, "
                  "playlist or app.",
    "next_track": "Skip to the next song, track or episode.",
    "previous_track": "Go back to the previous song or track.",
    "volume_up": "Make the sound louder.",
    "volume_down": "Make the sound quieter.",
    "mute": "Mute the sound.",
    "unmute": "Unmute the sound.",
    "set_volume": "Set the volume to a specific level or percentage.",
    "dark_mode": "Turn dark mode on for the whole Mac.",
    "light_mode": "Turn dark mode off, or light mode on, for the whole Mac.",
    "time": "Ask what time it is here (not in another city or time zone).",
    "date": "Ask today's date or what day it is.",
    "battery": "Ask about the battery level or charging.",
    "other": "Anything else: several steps, a question needing knowledge, a specific website, song, video, file, person, "
             "message, email or search, screen brightness, clicking on the screen, or anything risky like quitting, "
             "deleting or sending.",
}

# Asked with INTENTS: is it one simple action? Its examples name some of INTENTS, so keep the two in step.
SINGLE = dict(instructions="Is 'request' just one simple action or question, like opening an app, pausing, changing the "
                           "volume or asking the time? Words that only give a reason, context or politeness don't count "
                           "as more.",
              criteria={"single": "One simple action or question, maybe with a reason, context or politeness around it.",
                        "more": "More than one action, or one with details a simple action can't honour: a specific "
                                "song, playlist, video or website, a place or time zone, something to write, a person "
                                "to message, or a setting for only one app."})

# Asked with INTENTS: can Claude answer by talking alone? Then a faster model takes the turn (brain.py, "chat_model").
TALK = dict(instructions="Can a Mac assistant answer 'request' (a reply to 'assistant_just_said', if given) just by "
                         "talking, or does it need the computer?",
            criteria={"talk": "Only conversation or general knowledge it already knows: a greeting, small talk, a "
                              "joke, advice, an opinion, an explanation, maths, or a fact that doesn't change.",
                      "computer": "Needs the Mac or the internet: the screen, apps, windows, files, music, messages, "
                                  "email, the web, settings, or anything to do, open, find, check or look up, or "
                                  "anything current like news, weather, prices or the time."})
TALK_MIN = 0.9             # below this, the normal model answers

# Asked by judge() about one action described in words (safety.py), not about your request: the rule is about the action.
# "deletes" names lists, menu items and delete keys: without them cmd+backspace in Finder scored 0.71 to 0.77 and
# alt+cmd+backspace 0.52 to 0.62 (tests/test_jev_safety_live.py).
RISK = {
    "deletes": "Deletes, removes or discards something: files, folders, emails, messages, chats, photos, contacts, "
               "events, history, items in a list or playlist, unsaved changes (Don't Save, Discard), emptying a trash "
               "or bin, Delete or Remove buttons and menu items, delete keys on selected items (backspace, "
               "cmd+backspace, alt+cmd+backspace).",
    "overwrites": "Replaces or overwrites existing data: saving over an existing file, Replace in a save dialog, "
                  "editing an existing file, resetting or restoring something.",
    "safe": "Anything else: opening, reading, searching, navigating, scrolling, typing new text, playing, sending, "
            "creating something new.",
}

# Obvious actions: shown in the chat but not spoken
SILENT = {"open_app", "play_pause", "next_track", "previous_track", "mute", "unmute", "dark_mode", "light_mode"}

# NX_KEYTYPE_* codes for the keyboard's media keys
MEDIA_KEYS = {"play_pause": 16, "next_track": 17, "previous_track": 18}


def load_key():
    """TYPESAFE_API_KEY from the environment, or from the private .env file next to this one."""
    if os.environ.get("TYPESAFE_API_KEY"):
        return True
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "TYPESAFE_API_KEY" and value.strip():
                os.environ["TYPESAFE_API_KEY"] = value.strip()
                return True
    return False


def installed_apps():
    dirs = ["/Applications", "/System/Applications", "/System/Applications/Utilities", os.path.expanduser("~/Applications")]
    return sorted({os.path.basename(p)[:-4] for d in dirs for p in glob.glob(f"{d}/*.app")})


def osascript(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5).stdout.strip()


def press_media_key(code):
    """Same as pressing play/pause, next or previous on the keyboard: controls whatever is playing."""
    import Quartz
    from AppKit import NSEvent
    for down in (True, False):
        state = 0xA if down else 0xB
        event = NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
            14, (0, 0), state << 8, 0, 0, None, 8, (code << 16) | (state << 8), -1)  # 14 = system-defined, 8 = aux key
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, event.CGEvent())


def volume_level(text):
    """The level asked for in "set volume to 30" / "half volume" / "max volume", or None. Also None if there's more
    than one number or level word ("it's 2 am, set the volume to 20"): Claude works out which one is meant."""
    t = text.lower()
    words = {"half": 50, "max": 100, "maximum": 100, "full": 100, "zero": 0, "quarter": 25}
    found = [int(n) for n in re.findall(r"\b\d{1,3}\b", t)] + [v for w, v in words.items() if re.search(rf"\b{w}\b", t)]
    return max(0, min(100, found[0])) if len(found) == 1 else None


def dark_mode_script(intent):
    """AppleScript for dark_mode (on) or light_mode (off). Jev picks which, so "the lights are off" can't flip it."""
    on = "true" if intent == "dark_mode" else "false"
    return f'tell application "System Events" to tell appearance preferences to set dark mode to {on}'


def do(intent, text, app):
    """Carry out a simple command. Returns the result text for the chat, or None to hand it to Claude instead.
    Only answers are spoken (see SILENT): you can already see or hear that an app opened or the music paused."""
    if intent == "open_app":
        subprocess.run(["open", "-a", app], check=True, timeout=10)
        return f"Opened {app}."
    if intent in MEDIA_KEYS:
        press_media_key(MEDIA_KEYS[intent])
        return {"play_pause": "Play/pause.", "next_track": "Next track.", "previous_track": "Previous track."}[intent]
    if intent == "set_volume" and volume_level(text) is None:
        return None  # no level, or more than one number ("it's 2 am, set the volume to 20"): let Claude ask
    if intent in ("volume_up", "volume_down", "set_volume"):
        now = int(osascript("output volume of (get volume settings)") or 50)
        level = {"volume_up": min(100, now + 10), "volume_down": max(0, now - 10)}.get(intent, volume_level(text))
        osascript(f"set volume output volume {level} without output muted")
        return f"Volume {level}%."
    if intent in ("mute", "unmute"):
        osascript(f"set volume {'with' if intent == 'mute' else 'without'} output muted")
        return "Muted." if intent == "mute" else "Unmuted."
    if intent in ("dark_mode", "light_mode"):
        osascript(dark_mode_script(intent))
        return "Dark mode on." if intent == "dark_mode" else "Light mode on."
    if intent == "time":
        return "It's " + datetime.datetime.now().strftime("%-I:%M %p") + "."
    if intent == "date":
        return "It's " + datetime.datetime.now().strftime("%A, %-d %B") + "."
    if intent == "battery":
        out = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"(\d+)%;\s*([a-z ]+);", out)
        if not m:
            return None
        pct, status = m.groups()
        charging = {"charging": " and charging", "charged": ", fully charged", "discharging": ""}.get(status.strip(), "")
        return f"Battery is at {pct}%{charging}."
    return None


def took(t0):
    return f"{time.monotonic() - t0:.2f}s"


def why_claude(answers):
    """Why Jev's answers send the request to Claude (a short reason, no request text), or None if Mac does it itself."""
    intent, app, single = (answers.get(k) for k in ("intent", "app", "single"))
    if None in (intent, app, single):
        return "incomplete answer"  # the API can leave an answer out
    if intent.choice == "other":
        return "other"
    if intent.confidence < MIN_CONFIDENCE:
        return f"unsure ({intent.choice} {intent.confidence:.2f})"
    if (p := single.probabilities.get("single", 0)) < SINGLE_MIN:
        return f"not one simple action ({p:.2f})"  # e.g. "open Spotify and play some lofi"
    if intent.choice == "open_app" and (app.choice == "none" or app.confidence < APP_MIN_CONFIDENCE):
        return f"app {app.choice} {app.confidence:.2f}"
    return None


class Router:
    def __init__(self, log, fast_path=True):
        """fast_path: False turns route() off; judge() still works."""
        self.log = log
        self.enabled = load_key()
        self.fast_path = fast_path
        self.chat = False
        self._early = None  # ((text, reply_to), Jev's answer as a task) from prefetch()
        self.last_call = 0.0  # time.monotonic() of the last call to Jev
        self.client = None
        self._connecting = asyncio.Lock()  # warm-up and the first judge() can't each open a client
        self.apps = installed_apps()

    async def route(self, text, reply_to=None):
        """(intent, result, speak it?) if Mac handled it instantly, ("ignore", None) if it was background talk heard
        while waiting for your reply, or None to send it to Claude.
        reply_to: what Mac just said, when this was heard in the listen-after-reply window (no wake word)."""
        self.chat = False  # chat_model: Jev is sure it's only conversation
        if not (self.enabled and self.fast_path):
            return None
        t0 = time.monotonic()
        early, self._early = self._early, None
        reused = early is not None and early[0] == (text, reply_to)
        if early and not reused:
            early[1].cancel()  # you said more after the early transcript
        try:
            answers = await asyncio.wait_for(early[1] if reused else self._ask(text, reply_to), TIMEOUT_S)
        except asyncio.TimeoutError:
            self.log(f"fast path: Jev too slow, using Claude, {took(t0)}")
            return None
        except Exception as e:
            if type(e).__name__ == "TypeSafeAuthenticationError":
                self.enabled = False
                self.log(f"fast path: the Typesafe API key was rejected, turning the fast path off, {took(t0)}")
            else:
                self.log(f"fast path: {type(e).__name__}, using Claude, {took(t0)}")
            return None
        jev = ("early, " if reused else "") + took(t0)  # how long the request waited for Jev
        talk = answers.get("talk")
        self.chat = talk is not None and talk.probabilities.get("talk", 0) >= TALK_MIN
        kind = answers.get("kind")  # missing: treat it as meant for Mac rather than drop what was said
        if reply_to is not None and kind is not None and (p := kind.probabilities.get("for_assistant", 0)) < FOR_ME_MIN:
            self.log(f"fast path: ignored background talk ({p:.2f}), {jev}")
            return "ignore", None
        if why := why_claude(answers):
            self.log(f"fast path: {why}{', chat' if self.chat else ''}, {jev}")
            return None
        intent, app = answers["intent"], answers["app"]
        try:
            reply = await asyncio.to_thread(do, intent.choice, text, app.choice)
        except Exception as e:
            self.log(f"fast path: {intent.choice} failed ({e!r}), using Claude, {jev}")
            return None
        if reply:
            self.log(f"fast path: {intent.choice} ({intent.confidence:.2f}), {jev}")
            return intent.choice, reply, intent.choice not in SILENT
        self.log(f"fast path: {intent.choice} needs Claude, {jev}")  # e.g. "set the volume" without a level
        return None

    def prefetch(self, text, reply_to=None):
        """early_jev: ask Jev about the early final transcript while the end of your speech is still being waited
        out. route() uses that answer if the final text is the same. Call on the brain's event loop."""
        if self.enabled and self.fast_path:
            if self._early:
                self._early[1].cancel()
            task = asyncio.ensure_future(self._ask(text, reply_to))
            task.add_done_callback(lambda t: t.cancelled() or t.exception())  # an unused failure isn't an error
            self._early = (text, reply_to), task

    async def judge(self, action):
        """Does this action (described in words by safety.py) delete or overwrite something? (p_risky, top choice),
        p_risky = p(deletes) + p(overwrites). Raises if Jev can't answer (no key, error, timeout): safety.py then uses
        its local rules."""
        from typesafe_sdk import Choice
        if not self.enabled:
            raise RuntimeError("no Typesafe key")

        async def ask():
            self.last_call = time.monotonic()
            client = await self._connected()
            return await client.system_one(
                state={"action": action},
                questions={"risk": Choice(instructions="A Mac assistant is about to do 'action'. Does it delete or "
                                                       "overwrite something?", criteria=RISK)},
                model="jev-latest", timeout=JUDGE_TIMEOUT_S)
        try:
            r = await asyncio.wait_for(ask(), JUDGE_TIMEOUT_S)
        except Exception as e:
            if type(e).__name__ == "TypeSafeAuthenticationError":
                self.enabled = False
                self.log("safety: the Typesafe API key was rejected, using local rules from now on")
            raise
        risk = r.answers.get("risk")
        if risk is None or any(k not in risk.probabilities for k in RISK):  # a missing choice isn't a 0
            raise ValueError("incomplete answer")
        p = risk.probabilities
        return p["deletes"] + p["overwrites"], risk.choice

    async def _connected(self):
        from typesafe_sdk import AsyncTypeSafeClient
        async with self._connecting:
            if self.client is None:
                self.client = await AsyncTypeSafeClient().__aenter__()  # one connection, kept open: faster
        return self.client

    async def _ask(self, text, reply_to=None):
        from typesafe_sdk import Choice
        self.last_call = time.monotonic()
        await self._connected()
        state, extra = {"request": text}, {}
        if reply_to is not None:
            state["assistant_just_said"] = reply_to
            extra["kind"] = Choice(
                instructions="A voice assistant on a Mac just said 'assistant_just_said', then its microphone picked up "
                             "'request' (speech-to-text, so it may contain mistakes). What is 'request'?",
                criteria={"for_assistant": "A sensible reply to the assistant, or a clear request or question for it.",
                          "gibberish": "Garbled or meaningless words, mixed languages, or broken sentences that make no sense.",
                          "background": "A sensible sentence that isn't aimed at the assistant: TV, a song, or someone "
                                        "talking to another person."})
        r = await self.client.system_one(
            state=state,
            questions=extra | {
                "intent": Choice(instructions="What does the user want their Mac assistant to do? Pick 'other' unless "
                                              "the request is exactly one of the simple actions (a reason, context or "
                                              "politeness around it is fine).", criteria=INTENTS),
                "app": Choice(instructions="If the user wants to open an app, which installed app? 'none' if not installed "
                                           "or not about opening an app.",
                              criteria={a: None for a in self.apps} | {"none": "No installed app matches."}),
                "single": Choice(**SINGLE),
                "talk": Choice(**TALK),
            },
            model="jev-latest", timeout=TIMEOUT_S)
        return r.answers

    async def warm_up(self):
        """Open the connection at startup, so the first real request isn't slowed by it (and doesn't time out)."""
        if not self.enabled:
            return
        try:
            t0 = time.monotonic()
            await asyncio.wait_for(self._ask("what time is it"), 15)
            self.log(f"fast path: ready, {took(t0)}")
        except Exception as e:
            self.log(f"fast path: warm-up failed ({type(e).__name__}), will retry on the first request")

    async def warm(self):
        """keep_warm: listening started. If Jev has been unused for a while, one small call now, while you talk, so
        your request doesn't pay for a cold connection."""
        if self.enabled and time.monotonic() - self.last_call > WARM_AFTER_S:
            self.last_call = time.monotonic()  # one warm-up, however often listening starts meanwhile
            await self.warm_up()

    async def close(self):
        if self.client:
            await self.client.aclose()
