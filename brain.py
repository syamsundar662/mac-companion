"""The companion's brain: a Claude Agent SDK session that drives the Mac through the mac-computer MCP server.

Runs on its own asyncio thread. Talks to the UI only through the `ui` object, whose methods hop to the main thread.
"""
import asyncio
import datetime
import itertools
import os
import re
import sys
import threading
import time
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient, HookMatcher, PermissionResultAllow,
    PermissionResultDeny, ResultMessage, StreamEvent, TextBlock, create_sdk_mcp_server, tool,
)

import ax
from router import Router, took
from safety import Safety, full_path

LANGUAGES = {"en": "English", "ta": "Tamil", "ml": "Malayalam"}

HERE = Path(__file__).resolve().parent
PERSONA = HERE / "persona.md"
MEMORY = HERE / "memory.md"
LOG = HERE / "companion.log"
MAC_SERVER = HERE / "hands/server.py"
KILL_SWITCH = "~/.mac-companion/STOP"  # while this file exists, the mac tools refuse to act (kill_switch_file)

BUILTIN_TOOLS = ["Bash", "Read", "Glob", "Grep", "Write", "Edit", "WebSearch", "WebFetch"]
READ_ONLY = {"Read", "Glob", "Grep", "WebSearch", "WebFetch"}
# mac and click-by-name tools that touch the screen: the chat panel steps aside before these run.
HANDS = {"screenshot", "click", "move", "drag", "scroll", "type", "key", "open_app",
         "ui_controls", "ui_press", "ui_set_text", "ui_menu"}
# keep_warm: sent when you start talking after a quiet spell; the reply is dropped.
WARM_PROMPT = "[{user} is about to ask you something. Reply with only: ok]"
WARM_TIMEOUT_S = 30  # a stuck warm-up mustn't hold the lock (and every request behind it) for good
CHECKED_UI = {"mcp__companion__ui_press", "mcp__companion__ui_set_text", "mcp__companion__ui_menu"}  # safety.py first


def log(msg):
    with open(LOG, "a") as f:
        f.write(datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")


def touches_screen(name, args):
    """mac tools that use the screen, and commands that may open or script an app: the chat panel steps aside first."""
    return (name.startswith(("mcp__mac__", "mcp__companion__")) and name.split("__")[-1] in HANDS) or (
        name == "Bash" and bool(re.search(r"\b(open|osascript)\b", args.get("command", ""))))


def describe(name, a):
    """One short line for the status bubble."""
    short = name.split("__")[-1]
    if short == "screenshot":
        return "Looking at the screen"
    if short == "click":
        return f"Clicking at {a.get('x')}, {a.get('y')}"
    if short == "type":
        t = a.get("text", "")
        return f"Typing \"{t[:40]}{'...' if len(t) > 40 else ''}\""
    if short == "key":
        return f"Pressing {a.get('combo')}"
    if short in ("scroll", "drag", "move"):
        return {"scroll": "Scrolling", "drag": "Dragging", "move": "Moving the mouse"}[short]
    if short == "open_app":
        return f"Opening {a.get('target')}"
    if short in ("frontmost", "displays"):
        return "Checking the screen"
    if name == "Bash":
        return a.get("description") or f"Running {a.get('command', '')[:50]}"
    if name == "WebSearch":
        return f"Searching the web for {a.get('query')}"
    if name == "WebFetch":
        return "Reading a web page"
    if name in ("Read", "Glob", "Grep"):
        return "Looking through files"
    if name in ("Write", "Edit"):
        return f"Editing {Path(a.get('file_path', '')).name}"
    if short == "remember":
        return "Saving that to memory"
    if short == "ui_controls":
        return f"Reading {a.get('app')}"
    if short == "ui_press":
        item = ax._cache.get(a.get("id")) or {}
        return f"Pressing {item['label']}" if item.get("label") else "Pressing a button"
    if short == "ui_set_text":
        t = a.get("text", "")
        return f"Typing \"{t[:40]}{'...' if len(t) > 40 else ''}\""
    if short == "ui_menu":
        return f"Choosing {a.get('path')}"
    return short


def said(text):
    return {"content": [{"type": "text", "text": text}]}


# Click by name (ax.py). AX calls block, so they run in a thread.
@tool("ui_controls", "List an app's buttons, fields, menus, rows and links by name, with ids for ui_press and "
      "ui_set_text. Ids are only good until the next ui_controls.", {"app": str})
async def ui_controls(args):
    items = await asyncio.to_thread(ax.list_controls, args["app"])
    if items is None:
        return said(f"No app called '{args['app']}' is running.")
    return said(ax.format_listing(items) or f"{args['app']} shows no controls that can be read. Use a screenshot.")


@tool("ui_press", "Press a control by its id from the latest ui_controls.", {"id": int})
async def ui_press(args):
    ok = await asyncio.to_thread(ax.press, args["id"])
    return said("Pressed." if ok else "That didn't work: the id isn't from the latest ui_controls, or the control "
                "can't be pressed. List again, or take a screenshot and click.")


@tool("ui_set_text", "Replace the text in a field by its id from the latest ui_controls.", {"id": int, "text": str})
async def ui_set_text(args):
    ok = await asyncio.to_thread(ax.set_text, args["id"], args["text"])
    return said("Done." if ok else "That didn't work: the id isn't from the latest ui_controls, or the field doesn't "
                "take text this way. Click it and use type instead.")


@tool("ui_menu", "Choose an item from an app's menu bar by its path, like 'File > New Window'.", {"app": str, "path": str})
async def ui_menu(args):
    return said(await asyncio.to_thread(ax.menu, args["app"], args["path"]) or "Done.")


UI_TOOLS = [ui_controls, ui_press, ui_set_text, ui_menu]


class ReplyStream:
    """Claude's reply as it is written (one turn's top-level stream events), so it can be spoken sentence by sentence:
    ui.reply_delta(text); ui.reply_end() once the reply is complete; ui.reply_discard() when the text so far turns out
    to be narration before a tool call (only the final result is spoken)."""

    def __init__(self, ui):
        self.ui, self.texts = ui, 0

    def feed(self, msg):
        if msg.parent_tool_use_id is not None:
            return  # a subagent's stream
        ev = msg.event
        kind, delta = ev.get("type"), ev.get("delta") or {}
        if kind == "content_block_start":
            block = (ev.get("content_block") or {}).get("type") or ""
            if block == "text":
                if self.texts:  # a sentence boundary, so two blocks aren't glued together ("that.It's")
                    self.ui.reply_delta("\n")
                self.texts += 1
            elif block.endswith("tool_use"):
                self.ui.reply_discard()
        elif kind == "content_block_delta" and delta.get("type") == "text_delta":
            self.ui.reply_delta(delta.get("text", ""))
        elif kind == "message_delta":
            if delta.get("stop_reason") == "end_turn":
                self.ui.reply_end()
            elif delta.get("stop_reason") == "tool_use":
                self.ui.reply_discard()


class Brain:
    model = None  # the model the session runs on now (chat_model switches it per turn)
    last_claude = 0.0  # time.monotonic() Claude last answered (a turn or a keep_warm warm-up)
    warming = False  # keep_warm's "ok" turn is running: no tools, and a stop interrupts it
    _started = _stopped = 0  # requests started; a stop ends every request started so far

    def __init__(self, cfg, ui):
        self.cfg, self.ui = cfg, ui
        self.client = None
        self.busy = False
        self._interrupted = False
        self._approvals = {}
        self._ids = itertools.count(1)
        # Jev: instant commands (route, if "fast_path") and what needs your yes (safety.py, if "jev_safety")
        router = Router(log, cfg.get("fast_path", True))
        self.router = router if router.enabled else None  # enabled: there's a Typesafe key
        self.safety = Safety(self.router, log, cfg.get("jev_safety", True))
        self.local_notes = []  # simple commands done instantly without Claude, to mention in the next request
        self.last_reply = ""
        self.last_active = time.monotonic()
        self.turns_since_fresh = 0
        self.lock = asyncio.Lock()
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self._submit(self._init())

    # ---- called from the UI thread ----

    def send(self, text, app_name, reply_to_last=False, lang="en"):
        """reply_to_last: heard right after Mac spoke, without the wake word (might be background talk).
        lang: en / ta / ml, what the request was spoken or typed in."""
        self._submit(self._turn(text, app_name, reply_to_last, lang))

    def prefetch(self, text, reply_to_last=False, lang="en"):
        """early_jev: what you said so far (the early final transcript), so Jev is asked before listening ends."""
        if self.router and lang == "en":
            self.loop.call_soon_threadsafe(self.router.prefetch, text, self.last_reply if reply_to_last else None)

    def warm(self):
        """keep_warm: listening started; wake Jev and Claude if they've been idle (at most one call each)."""
        self._submit(self._warm())

    def answer(self, rid, ok):
        fut = self._approvals.get(rid)
        if fut:
            self.loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(ok))

    def stop(self):
        self._submit(self._interrupt())

    def reset(self):
        self._submit(self._reset())

    def shutdown(self):
        if self.router:
            try:
                self._submit(self.router.close()).result(timeout=2)
            except Exception:
                pass
        if self.client:
            try:
                self._submit(self.client.disconnect()).result(timeout=3)
            except Exception:
                pass

    # ---- brain thread ----

    def _submit(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def _init(self):
        if self.router and (self.cfg.get("fast_path", True) or self.cfg.get("jev_safety", True)):
            asyncio.ensure_future(self.router.warm_up())
        asyncio.ensure_future(self._keep_fresh())
        async with self.lock:
            await self._connect()

    async def _connect(self, quiet=False):
        try:
            client = ClaudeSDKClient(self._options())
            await client.connect()
            self.client, self.model = client, self.cfg.get("model") or None
            if not quiet:
                self.ui.connected(None)
        except Exception as e:
            log(f"connect failed: {e!r}")
            self.client = None
            self.ui.connected(f"Couldn't start Claude: {e}")

    @property
    def stop_file(self):
        return Path(os.path.expanduser(self.cfg.get("kill_switch_file") or KILL_SWITCH))

    def _mac_env(self):
        # wait this long after each click or keypress before the screenshot (the server's default is 1 s)
        env = {"MAC_COMPUTER_PAUSE": str(self.cfg.get("step_pause", 0.5)), "MAC_COMPUTER_STOP": str(self.stop_file)}
        if self.cfg.get("cliclick"):
            env["MAC_COMPUTER_CLICLICK"] = os.path.expanduser(self.cfg["cliclick"])
        if os.environ.get("MAC_COMPUTER_MIC_GUARD"):
            env["MAC_COMPUTER_MIC_GUARD"] = os.environ["MAC_COMPUTER_MIC_GUARD"]
        return env

    @property
    def user(self):
        """The user's name ("the user" when config.json has none), and the same with a capital for a sentence start."""
        user = self.cfg.get("user_name") or "the user"
        return user, user[:1].upper() + user[1:]

    def _languages(self, user, User):
        codes = self.cfg.get("languages") or ["en"]
        if len(codes) < 2:
            return ""
        names = [LANGUAGES.get(c, c) for c in codes]
        other = " and ".join(n for n in names if n != "English")
        how = (" Tamil in Tamil script." if "ta" in codes else "") + (
            " Malayalam in Manglish (Malayalam words written in English\nletters, the way people type it on WhatsApp), "
            "because there is no Malayalam voice to read Malayalam script aloud." if "ml" in codes else "")
        return f"""
# Languages
{User} speaks {", ".join(names[:-1])} and {names[-1]}. A request that wasn't in English says which language it was in.
Reply in the language {user} used.{how}
Speech-to-text in {other} can garble English words like app names; work out what was meant.
"""

    def _system_prompt(self):
        name, (user, User) = self.cfg["name"], self.user
        persona = PERSONA.read_text().strip() if PERSONA.exists() else ""
        memory = MEMORY.read_text().strip() if MEMORY.exists() else ""
        today = datetime.datetime.now().strftime("%A, %d %B %Y")
        by_name = self.cfg.get("ax_tools", True)
        hands = "screenshot, click, type, key, scroll, drag, move, open_app, frontmost, displays" + (
            ", and click by name: ui_controls, ui_press, ui_set_text, ui_menu" if by_name else "")
        first = ("\n- First try `ui_controls` and `ui_press` / `ui_set_text` / `ui_menu`. Use screenshots and clicks only "
                 "when an app doesn't expose the control, or to check what's on screen." if by_name else "")
        # natural: replies that sound like a friend talking, not an assistant writing
        answers = (f"""When {user} asks something, answer like a friend talking out loud: the answer in the first few
  words, one short sentence, two at most, then stop. Contractions (it's, you're, don't) and everyday words. No formal
  or filler phrases: never "Certainly", "Of course", "Absolutely", "Great question", "I'd be happy to", "As an AI" or
  "Is there anything else".""" if self.cfg.get("natural", True) else
                   f"When {user} asks a question, give the answer in one short sentence.")
        return f"""You are {name}, {user}'s companion. You live on {user}'s computer (a Mac) and can see and control it.
{persona}

# How you talk
- {User} usually talks to you out loud: the Mac's microphone hears them and their speech reaches you as text. So you
  can hear {user}. Never say you can't hear them, have no mic, or only read text; if they ask "can you hear me?", say yes.
- Your replies appear in a small chat bubble and are read aloud. No markdown, lists, headings or code unless {user} asks.
- Never narrate. Don't say what you're doing or about to do ("On it", "Opening Spotify", "Let me check", "Clicking now",
  "Found it"). Work silently.
- When a task is done, reply with only the result in as few words as possible, or just "Done." if the result is
  obvious on screen.
- {answers}
- Never use em dashes or en dashes.
- After you reply, {user} can answer straight away without saying your name, so a short follow-up question is fine when you need more details.

# Controlling the Mac
Your hands are the mac tools: {hands}.
- Take the fastest route: open_app, `open` with a URL or file, keyboard shortcuts, or AppleScript through Bash `osascript` for scriptable apps (Music, Spotify, Safari, Finder, Notes, Reminders, Calendar, System Events). Use screenshots and clicks when there is no quicker way.{first}
- Before clicking, take a screenshot and convert coordinates with the formula it gives you. Never guess coordinates. Check the fresh screenshot after each action before the next one.
- Always pass expect_app to type and key.
- The {name} icon in the menu bar and any small {name} bubble on screen are you. Ignore them and never click them.
- {User} shares this keyboard and mouse. If focus jumps or something unexpected appears, stop and tell them briefly.
- If a tool says the STOP file is present, stop at once and tell {user} the kill switch is on.

# Safety
{User} wants you to just go ahead with everything, including sending messages and emails, posting, buying, submitting
forms, changing settings and quitting apps. Don't ask for permission: Mac checks every action itself and asks {user}
before anything that deletes or overwrites. If an action comes back refused because {user} said no, don't try it
another way; ask what they'd like instead. When typing into messaging apps, pass allow_messaging=true. Never type
passwords, card numbers or one-time codes; ask {user} to type those.
{self._languages(user, User)}
# Memory
When you learn a lasting preference or fact about {user} (favourite apps, playlists, people they mention, how they like things done), save it with the remember tool. Never save secrets.
<memory>
{memory}
</memory>

Simple commands (opening an app, play/pause/next, volume, mute, dark mode, the time, the date, battery) are done
instantly before they reach you. Your next request mentions them in brackets, so you know what already happened.

Today is {today}. Each request says which app {user} was using when they called you."""

    def _options(self):
        @tool("remember", "Save one lasting fact or preference about the user to long-term memory.", {"fact": str})
        async def remember(args):
            fact = " ".join(args["fact"].split())
            with open(MEMORY, "a") as f:
                f.write(f"- {fact}\n")
            return {"content": [{"type": "text", "text": "Saved."}]}

        return ClaudeAgentOptions(
            system_prompt=self._system_prompt(),
            model=self.cfg.get("model") or None,
            effort=self.cfg.get("effort") or None,  # "low" = minimal thinking, fastest replies
            thinking={"type": "disabled"} if self.cfg.get("thinking") == "off" else None,
            tools=BUILTIN_TOOLS,
            include_partial_messages=bool(self.cfg.get("stream_speech", True)),  # the reply as it is written
            mcp_servers={
                "mac": {"type": "stdio", "command": sys.executable, "args": [str(MAC_SERVER)], "env": self._mac_env()},
                "companion": create_sdk_mcp_server(
                    "companion", tools=[remember] + (UI_TOOLS if self.cfg.get("ax_tools", True) else [])),
            },
            strict_mcp_config=True,
            setting_sources=[],
            permission_mode="default",
            can_use_tool=self._can_use_tool,
            # Long timeout: the hook may wait while you have the panel open (the CLI default is 60 s).
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[self._pre_tool], timeout=600)]},
            cwd=str(Path.home()),
            max_buffer_size=64 * 1024 * 1024,  # screenshots arrive as multi-MB messages; the default is 1 MB
            stderr=lambda line: log(f"cli: {line}"),
        )

    async def _pre_tool(self, inp, tool_use_id, ctx):
        """Runs before _can_use_tool for the same call (the CLI awaits PreToolUse hooks first), so the panel is out of
        the way when safety.py reads the screen."""
        name, args = inp["tool_name"], inp["tool_input"]
        if self.warming:  # keep_warm's turn shows nothing and needs no safety checks
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": "Reply with only: ok"}}
        self.ui.activity(describe(name, args))
        if reason := await self._blocked(name, args):
            return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                           "permissionDecisionReason": reason}}
        return {}

    async def _blocked(self, name, args):
        """Why this tool can't run now (stopped, or you're using the chat panel), or None. Waits for the panel to step
        aside first if the tool uses the screen."""
        if touches_screen(name, args) and not await self._clear_screen():
            return "Stopped by the user." if self._interrupted else "The user is looking at the chat panel. Try again in a moment."
        return "Stopped by the user." if self._interrupted else None

    async def _clear_screen(self):
        """Wait until the chat panel is out of the way. False if it stayed open (you're looking at it)."""
        done = self.loop.create_future()
        self.ui.clear_screen(lambda: self.loop.call_soon_threadsafe(lambda: done.done() or done.set_result(None)))
        try:
            await asyncio.wait_for(done, 300)
            return True
        except asyncio.TimeoutError:
            return False

    async def _can_use_tool(self, name, args, ctx):
        no = PermissionResultDeny(message=f"{self.user[1]} said no. Don't do it; ask what they'd like instead.")
        if (name.startswith("mcp__companion__") and name not in CHECKED_UI) or name in READ_ONLY:
            return PermissionResultAllow()  # remember, ui_controls, reading
        if name.startswith("mcp__mac__") or name in CHECKED_UI or name == "Bash":  # Jev decides what needs a yes
            card = await self.safety.check(name, args)
            if not card:
                return PermissionResultAllow()
            if not await self._ask(*card):
                return no
            # The card opened the chat panel: it must be out of the way again before Mac uses the screen.
            reason = await self._blocked(name, args)
            return PermissionResultDeny(message=reason) if reason else PermissionResultAllow()
        if name == "Write":
            path = args.get("file_path", "")
            if not os.path.exists(full_path(path)):
                return PermissionResultAllow()  # a new file
            ok = await self._ask("Overwrite this existing file?", path)
            return PermissionResultAllow() if ok else no
        if name == "Edit":
            ok = await self._ask("Change this existing file?", args.get("file_path", ""))
            return PermissionResultAllow() if ok else no
        return PermissionResultDeny(message=f"{name} isn't available to the companion.")

    async def _ask(self, title, detail):
        rid = next(self._ids)
        fut = self.loop.create_future()
        self._approvals[rid] = fut
        self.ui.approval(rid, title, detail)
        try:
            return await fut
        finally:
            self._approvals.pop(rid, None)

    async def _turn(self, text, app_name, reply_to_last=False, lang="en"):
        self.last_active = time.monotonic()
        self._started += 1
        me = self._started  # a stop while Jev decides or while waiting for the lock (keep_warm) ends this request
        # Jev sees every English request first. Tamil and Malayalam still go straight to Claude: Jev isn't reliable
        # on them yet ("pause" 0.81 to 0.88, Tamil "dark mode off" read as on; tests/test_router_live.py).
        # Outside the lock, so a Claude warm-up (keep_warm) doesn't hold up instant commands.
        routed, chat, t0 = None, False, time.monotonic()
        if self.router and lang == "en":
            try:
                routed = await self.router.route(text, self.last_reply if reply_to_last else None)
                chat = self.router.chat
            except Exception as e:  # never lose a request to a fast-path bug: Claude takes it
                log(f"fast path: {type(e).__name__}, using Claude, {took(t0)}")
        if routed and routed[0] == "ignore":
            self.ui.ignored(text)  # background talk heard while waiting for your reply
            return
        if routed:
            _, reply, speak = routed  # done instantly by the fast path, no Claude needed
            self.local_notes.append(f'"{text}": {reply}')
            self.last_reply = reply
            self.ui.assistant_text(reply)
            self.ui.turn_done(reply if speak else "", None, self._stopped >= me)  # obvious actions stay silent
            return
        async with self.lock:
            if self._stopped < me and not self.client:
                await self._connect()
                if not self.client:
                    self.ui.turn_done("", "Claude isn't connected. Check companion.log.", False)
                    return
            if self._stopped >= me:
                log("stopped before Claude started")
                self.ui.turn_done("", None, True)
                return
            self.busy, self._interrupted = True, False
            watcher = asyncio.create_task(self._watch_kill_switch())
            final, error = "", None
            stream = ReplyStream(self.ui)
            try:
                prompt = f"[{self.user[1]} was using {app_name}]\n{text}" if app_name else text
                if lang != "en":
                    prompt = f"[{self.user[1]} spoke {LANGUAGES.get(lang, lang)}]\n{prompt}"
                if self.local_notes:
                    prompt = f"[Done instantly since your last reply: {'; '.join(self.local_notes)}]\n{prompt}"
                    self.local_notes = []
                await self._use_model(chat)
                await self.client.query(prompt)
                async for msg in self.client.receive_response():
                    if isinstance(msg, StreamEvent):
                        stream.feed(msg)
                    elif isinstance(msg, AssistantMessage):
                        for b in msg.content:
                            if isinstance(b, TextBlock) and b.text.strip():
                                self.ui.assistant_text(b.text.strip())
                        if msg.error:
                            error = f"Claude error: {msg.error}"
                    elif isinstance(msg, ResultMessage):
                        final = (msg.result or "").strip()
                        if msg.is_error and not self._interrupted:
                            error = error or f"Something went wrong ({msg.subtype})."
            except Exception as e:
                log(f"turn failed: {e!r}")
                error = f"{type(e).__name__}: {e}"
                self.client = None  # reconnect on the next request
            finally:
                watcher.cancel()
                self.busy = False
                self.last_reply, self.last_active = final, time.monotonic()
                self.last_claude = self.last_active
                self.turns_since_fresh += 1
                self.ui.turn_done(final, error, self._interrupted)

    async def _use_model(self, chat):
        """chat_model: a turn Jev is sure is only conversation runs on a faster model (haiku: first words in about 1.0 s
        instead of 2.0 s). The same session either way, so the history is shared."""
        model = (chat and self.cfg.get("chat_model")) or self.cfg.get("model") or None
        if model != self.model:
            await self.client.set_model(model)
            self.model = model
            log(f"claude: {model or 'default model'}{' for a chat' if chat else ''}")

    async def _keep_fresh(self):
        """After a quiet spell, start a new conversation in the background. A long history (full of old screenshots)
        makes every reply slower; what matters long-term is in memory.md anyway."""
        idle_s = 60 * float(self.cfg.get("fresh_after_min", 10))
        while True:
            await asyncio.sleep(30)
            if self.busy or not self.turns_since_fresh or time.monotonic() - self.last_active < idle_s:
                continue
            async with self.lock:
                await self._fresh()

    async def _fresh(self):
        """A new conversation, without the old history (call it holding the lock)."""
        if self.client:
            try:
                await self.client.disconnect()
            except Exception as e:
                log(f"disconnect failed: {e!r}")
        self.client = None
        await self._connect(quiet=True)
        self.turns_since_fresh = 0
        self.last_reply = ""
        log("started a fresh conversation after a quiet spell")

    async def _warm(self):
        """keep_warm. Jev: unused for 2 minutes, one small call. Claude: unused for fresh_after_min, one tiny turn on
        the session while you talk. After a night's sleep the first request found a dead connection (8 s, then a
        retry) and a cold prompt cache (13.5k tokens), 11.7 s in all; that cost moves here, off your request."""
        if self.router and (self.cfg.get("fast_path", True) or self.cfg.get("jev_safety", True)):
            asyncio.ensure_future(self.router.warm())
        idle_s = 60 * float(self.cfg.get("fresh_after_min", 10))
        if self.busy or time.monotonic() - self.last_claude < idle_s:
            return
        self.last_claude = time.monotonic()  # one warm-up, however often listening starts meanwhile
        async with self.lock:
            if self.turns_since_fresh:
                await self._fresh()  # what _keep_fresh would do anyway; the warm-up goes to the new session
            if not self.client:
                await self._connect(quiet=True)
                if not self.client:
                    return
            t0 = time.monotonic()
            self.warming = True
            try:
                await asyncio.wait_for(self._warm_turn(), WARM_TIMEOUT_S)
                self.last_claude = time.monotonic()
                log(f"keep warm: Claude ready, {took(t0)}")
            except Exception as e:
                log(f"keep warm: Claude failed ({type(e).__name__}), {took(t0)}")
                self.client = None  # reconnect on the next request
            finally:
                self.warming = False

    async def _warm_turn(self):
        """The "ok" reply is read and dropped: not shown, not spoken, no timing line (that's in turn_done)."""
        await self._use_model(False)
        await self.client.query(WARM_PROMPT.format(user=self.user[1]))
        async for _ in self.client.receive_response():
            pass

    async def _watch_kill_switch(self):
        while True:
            await asyncio.sleep(0.5)
            if self.stop_file.exists():
                self.ui.activity("Kill switch is on, stopping")
                await self._interrupt()
                return

    async def _interrupt(self):
        self._interrupted, self._stopped = True, self._started
        for fut in list(self._approvals.values()):
            if not fut.done():
                fut.set_result(False)
        if (self.busy or self.warming) and self.client:  # warming: a request you stopped is waiting behind it
            try:
                await self.client.interrupt()
            except Exception as e:
                log(f"interrupt failed: {e!r}")

    async def _reset(self):
        await self._interrupt()
        async with self.lock:
            if self.client:
                try:
                    await self.client.disconnect()
                except Exception as e:
                    log(f"disconnect failed: {e!r}")
            self.client = None
            await self._connect()
