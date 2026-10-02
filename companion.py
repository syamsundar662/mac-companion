#!/usr/bin/env python3
"""Companion: a menu bar friend that sees and controls this Mac. Tap the hotkey (default ⌃⌥Space) and talk."""
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import objc
import Quartz
from AppKit import (
    NSApp, NSApplication, NSApplicationActivationPolicyAccessory, NSAppearance, NSBackingStoreBuffered, NSColor,
    NSEvent, NSEventMaskFlagsChanged, NSEventMaskKeyDown, NSEventMaskLeftMouseDown, NSEventMaskLeftMouseDragged,
    NSEventMaskMouseMoved, NSEventMaskOtherMouseDown, NSEventMaskRightMouseDown, NSEventMaskScrollWheel,
    NSEventModifierFlagCommand, NSEventModifierFlagControl, NSEventModifierFlagOption, NSEventModifierFlagShift,
    NSEventTypeFlagsChanged, NSEventTypeLeftMouseDragged, NSEventTypeMouseMoved, NSFloatingWindowLevel, NSFont, NSImage, NSMenu, NSMenuItem, NSPanel, NSScreen, NSSound, NSStatusBar,
    NSStatusWindowLevel, NSTextField, NSVariableStatusItemLength, NSViewHeightSizable, NSViewWidthSizable,
    NSVisualEffectBlendingModeBehindWindow, NSVisualEffectMaterialHUDWindow, NSVisualEffectStateActive,
    NSView, NSVisualEffectView, NSWindowCollectionBehaviorCanJoinAllSpaces, NSWindowCollectionBehaviorFullScreenAuxiliary,
    NSWindowCollectionBehaviorIgnoresCycle, NSWindowCollectionBehaviorMoveToActiveSpace,
    NSWindowCollectionBehaviorStationary, NSWindowSharingNone, NSWindowStyleMaskBorderless,
    NSWindowStyleMaskNonactivatingPanel, NSWorkspace,
)
from ApplicationServices import AXIsProcessTrusted
from Foundation import NSURL, NSMakeRect, NSObject, NSPointInRect
from PyObjCTools import AppHelper, MachSignals
from WebKit import WKWebView, WKWebViewConfiguration
from quickmachotkey import constants as K, mask, quickHotKey

from brain import Brain, log
from early_final import EarlyFinal
from speech_chunks import SentenceStreamer
from voice import END_SILENCE_S, SAY_MAX, Listener, Mic, Speaker, Transcriber, WakeSpotter, mentions, speakable, wake_rest
from glow import Glow
from voiceid import SENTENCES, VoiceID

HERE = Path(__file__).resolve().parent
CONFIG = HERE / "config.json"
DEFAULTS = {
    "name": "Mac", "user_name": "", "model": "sonnet", "hotkey": "ctrl+alt+space", "speak_replies": True,
    "voice": "", "voice_rate": 0, "whisper_model": "mlx-community/whisper-large-v3-turbo", "language": "en",
    "vocabulary": [], "wake_word": True, "wake_model": "mlx-community/whisper-base.en-mlx", "mic": "",
    "wake_aliases": [], "follow_up": True, "push_to_talk": "option", "fast_path": True, "jev_safety": True,
    "stream_speech": True, "early_final": True, "ax_tools": True,
    "voice_lock": False, "voice_match": 0.55, "ptt_sounds": True, "glow": True,
    "languages": ["en", "ta", "ml"], "voices": {"ta": "Vani", "ml": "Rishi"},
    "preview_model": "mlx-community/whisper-base-mlx",
    "one_pass_stt": True, "chat_model": "haiku", "early_jev": True, "keep_warm": True,
    "natural": True, "voice_adapt": True,
}
LOCAL_FILES = ("config.json", "persona.md", "memory.md")  # yours, made from the *.example files on first start
PANEL_W, PANEL_H = 600, 460
BUBBLE_H = 34
# The same palette as the panel: teal ready, rose listening, blue working, amber needs your OK
STATE_COLORS = {"idle": (0.43, 0.91, 0.85), "listening": (1.0, 0.48, 0.56), "transcribing": (0.49, 0.77, 1.0),
                "working": (0.49, 0.77, 1.0), "approval": (0.96, 0.72, 0.29)}
EARLY_S = 0.25                 # start the final transcription after this much quiet (early_final)
HOLD_S = 0.25                  # hold Option this long (alone) before it counts as "talking"
OPTION_SIDE = {"left_option": 0x20, "right_option": 0x40}  # device-specific bits in the modifier flags
DEBUG = bool(os.environ.get("COMPANION_DEBUG"))  # logs everything the wake word listener hears
ICONS = {"idle": "sparkles", "listening": "waveform", "transcribing": "ellipsis.bubble",
         "working": "wand.and.stars", "approval": "exclamationmark.bubble"}
# "No" wins over "yes", so "no, don't send it" and "not sure" are a no.
NO = re.compile(r"\b(no|not|nope|don'?t|do not|stop|cancel|deny|wait|never)\b", re.I)
# Saying only these (with or without "Hey Mac") means: stop everything and stop listening for a reply.
STOP_PHRASES = re.compile(
    r"\b(stop( it| listening| talking| now)?|cancel|never ?mind|wait|shut up|be quiet|quiet|enough|that'?s (all|it|enough)"
    r"|nothing|bye|good ?bye|leave it|go away|get out|go to sleep|sleep|no thanks?|thanks?|thank you|mic off"
    r"|turn off (the )?(mic|microphone)|(you'?re|you are) still listening)\b", re.I)
STOP_FILLERS = {"please", "okay", "ok", "just", "now", "hey", "hi", "it", "can", "you", "the", "hell", "out", "of", "here",
                "for", "me", "a", "so", "and", "that", "is", "all", "right", "alright", "no", "done", "i", "said", "already"}
MIC_OFF = re.compile(r"\b(listening|listen|mic|microphone|sleep)\b", re.I)


def script_language(text):
    """en / ta / ml by the alphabet used."""
    if re.search("[\u0B80-\u0BFF]", text):
        return "ta"
    if re.search("[\u0D00-\u0D7F]", text):
        return "ml"
    return "en"


def is_stop(text):
    """"stop", "stop it", "nothing", "that's all", "stop listening"... but not "stop the music" (that's a request)."""
    if not STOP_PHRASES.search(text):
        return False
    rest = re.findall(r"[a-z']+", STOP_PHRASES.sub(" ", text.lower()))
    return all(w in STOP_FILLERS for w in rest)
YES = re.compile(r"\b(yes|yeah|yep|yup|sure|ok|okay|go ahead|do it|allow|approve|confirm)\b", re.I)

MODS = {"ctrl": K.controlKey, "control": K.controlKey, "alt": K.optionKey, "option": K.optionKey,
        "shift": K.shiftKey, "cmd": K.cmdKey, "command": K.cmdKey}
MOD_SYMBOLS = [(K.controlKey, "⌃"), (K.optionKey, "⌥"), (K.shiftKey, "⇧"), (K.cmdKey, "⌘")]


def parse_hotkey(spec):
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    mods = [MODS[m] for m in mods]
    special = {"space": K.kVK_Space, "return": K.kVK_Return, "enter": K.kVK_Return}
    vk = special.get(key) or getattr(K, f"kVK_ANSI_{key.upper()}")
    label = "".join(sym for m, sym in MOD_SYMBOLS if m in mods) + (key.capitalize() if len(key) > 1 else key.upper())
    return vk, mask(*mods), label


def answer_timing(t_heard, t_submit, now, error=None, interrupted=False, approval=False):
    """The log line for how long a finished spoken request took, or None when there's nothing to time:
    typed (t_heard is None), failed or stopped. t_heard: (you stopped talking, listening ended).
    approval: it stopped to ask you, so the answer time includes your reply."""
    if t_heard is None or t_submit is None or error or interrupted:
        return None
    spoke, listened = t_heard
    return (f"timing: pause {listened - spoke:.1f}s, transcribe {t_submit - listened:.1f}s, "
            f"answer {now - t_submit:.1f}s" + (" (with approval)" if approval else ""))


def on_main(fn, *args):
    """Run fn on the main thread, logging instead of crashing on errors."""
    def run():
        try:
            fn(*args)
        except Exception:
            log("UI error:\n" + traceback.format_exc())
    AppHelper.callAfter(run)


class Panel(NSPanel):
    """Non-activating, like Spotlight: takes your typing while the app you were in stays active."""

    def canBecomeKeyWindow(self):
        return True


class Target(NSObject, protocols=[objc.protocolNamed("WKScriptMessageHandler")]):
    """Receives every Cocoa callback (launch, menu clicks, JS messages, window events) for the Controller."""

    def applicationDidFinishLaunching_(self, note):
        self.c = Controller(self)

    def applicationWillTerminate_(self, note):
        log("quit")
        self.c.shutdown()

    def userContentController_didReceiveScriptMessage_(self, ucc, message):
        on_main(self.c.on_js, json.loads(str(message.body())))

    def windowDidResignKey_(self, note):
        on_main(self.c.on_panel_resigned)

    def talk_(self, sender):
        self.c.hotkey()

    def stop_(self, sender):
        self.c.stop()

    def newChat_(self, sender):
        self.c.new_chat()

    def toggleSpeak_(self, sender):
        self.c.toggle_speak(sender)

    def toggleWake_(self, sender):
        self.c.toggle_wake(sender)

    def toggleGlow_(self, sender):
        self.c.toggle_glow(sender)

    def setupVoice_(self, sender):
        self.c.start_voice_setup()

    def toggleVoiceLock_(self, sender):
        self.c.toggle_voice_lock(sender)

    def toggleFollow_(self, sender):
        self.c.toggle_follow(sender)

    def togglePushToTalk_(self, sender):
        self.c.toggle_push_to_talk(sender)

    def editPersona_(self, sender):
        subprocess.run(["open", "-t", str(HERE / "persona.md")])

    def editMemory_(self, sender):
        subprocess.run(["open", "-t", str(HERE / "memory.md")])

    def showLog_(self, sender):
        (HERE / "companion.log").touch()
        subprocess.run(["open", "-a", "Console", str(HERE / "companion.log")])

    def quit_(self, sender):
        NSApp.terminate_(None)


class BrainUI:
    """What the brain may do to the UI. Called from the brain thread; every call hops to the main thread."""

    def __init__(self, c):
        self.c = c

    def connected(self, error):
        on_main(self.c.on_connected, error)

    def activity(self, text):
        on_main(self.c.on_activity, text)

    def assistant_text(self, text):
        on_main(self.c.on_assistant_text, text)

    def approval(self, rid, title, detail):
        on_main(self.c.on_approval, rid, title, detail)

    def clear_screen(self, done):
        on_main(self.c.on_clear_screen, done)

    def reply_delta(self, text):
        on_main(self.c.on_reply_delta, text)

    def reply_end(self):
        on_main(self.c.on_reply_end)

    def reply_discard(self):
        on_main(self.c.on_reply_discard)

    def turn_done(self, final, error, interrupted):
        on_main(self.c.on_turn_done, final, error, interrupted)

    def ignored(self, text):
        on_main(self.c.on_ignored, text)


def glass(frame, radius):
    v = NSVisualEffectView.alloc().initWithFrame_(frame)
    v.setMaterial_(NSVisualEffectMaterialHUDWindow)
    v.setBlendingMode_(NSVisualEffectBlendingModeBehindWindow)
    v.setState_(NSVisualEffectStateActive)
    v.setWantsLayer_(True)
    v.layer().setCornerRadius_(radius)
    v.layer().setMasksToBounds_(True)
    return v


def copy_examples(folder=HERE):
    for name in LOCAL_FILES:
        mine, example = folder / name, folder / name.replace(".", ".example.", 1)
        if not mine.exists() and example.exists():
            shutil.copyfile(example, mine)


def mouse_screen():
    m = NSEvent.mouseLocation()
    return next((s for s in NSScreen.screens() if NSPointInRect(m, s.frame())), NSScreen.mainScreen())


class Controller:
    def __init__(self, target):
        self.target = target
        copy_examples()
        self.cfg = {**DEFAULTS, **json.loads(CONFIG.read_text())}
        self.name = self.cfg["name"]
        self.vk, self.mods, self.hotkey_label = parse_hotkey(self.cfg["hotkey"])
        names = {p.strip().lower() for p in self.cfg["hotkey"].split("+")[:-1]}
        self.menu_mods = ((NSEventModifierFlagControl if names & {"ctrl", "control"} else 0)
                          | (NSEventModifierFlagOption if names & {"alt", "option"} else 0)
                          | (NSEventModifierFlagShift if "shift" in names else 0)
                          | (NSEventModifierFlagCommand if names & {"cmd", "command"} else 0))
        self.state = "idle"
        self.prev_app = None
        self.approval_id = None
        self.hands_active = False    # Mac is using the screen in this task (so your mouse or keys mean "stop")
        self.stopping = False        # a stop has been sent; the brain hasn't finished yet
        self.pending_request = None  # what you asked while it was stopping; runs as soon as it has
        self.stop_note = "Stopped"
        self.user_moved = 0.0
        self.js_ready, self.js_queue = False, []
        self.bubble_gen = 0
        self.bubble_timed = False
        self.last_activity = None
        self.wake_checking = False
        self.wake_pending = None     # a burst of speech heard while the previous one was being checked
        self.wake_names = [self.name.lower()] + [a.lower() for a in self.cfg["wake_aliases"]]
        self.request_lang = "en"
        self.preview_gen, self.preview_busy = 0, False  # live "what you're saying" while you talk
        self.preview_due = 0.0       # time.monotonic() from when the next live preview may start
        self.early = EarlyFinal()    # the final transcription, started at the first short pause
        self.voice_turn = False      # the current request was spoken (so keep the conversation going after)
        self.t_heard = None          # (you stopped talking, listening ended) for the last thing heard, for the log
        self.t_asked = self.t_submit = None  # t_heard (None if typed) and send time of the request the brain is on
        self.had_approval = False    # that request stopped to ask you (its answer time includes your reply)
        self.streamer = SentenceStreamer()  # the reply as Claude writes it, cut into sentences to speak
        self.spoke_stream = False    # some of this reply was spoken while it was written
        self.stream_chars = 0        # characters spoken that way this turn (capped like a whole reply)
        self.stream_t = None         # when you stopped talking, for the next streamed sentence's first-speech line
        self.following = False       # listening for a follow-up, no wake word needed
        self.ptt_down = False        # Option is held (alone)
        self.ptt_talking = False     # ...long enough that we're recording
        self.ptt_gen = 0
        self.ptt_since = None
        self.held = False            # this listen is a hold-to-talk
        self.ptt_choice = self.cfg["push_to_talk"] or "option"
        self._key_monitors = []
        self.voiceid = VoiceID(self.cfg["voice_match"])
        threading.Thread(target=self.voiceid.warm_up, daemon=True).start()
        self.enroll = None           # voice setup in progress: {"i": sentence number, "clips": [...]}

        self.speaker = Speaker(self.cfg["voice"], self.cfg["voice_rate"], on_note=log)
        self.mic = Mic(self.cfg["mic"])
        self.listener = Listener(self.mic, on_level=lambda x: on_main(self.on_level, x),
                                 # The times are read here, on the Listener's thread, before it can start again.
                                 on_done=lambda audio, err: on_main(self.on_heard, audio, err, self.listener.speech_end,
                                                                    self.listener.listen_end))
        self.transcriber = Transcriber(self.cfg["whisper_model"], self.cfg["wake_model"], self.cfg["language"],
                                       prompt=f"{self.name}, {', '.join(self.cfg['vocabulary'])}.",
                                       languages=self.cfg["languages"], preview_repo=self.cfg["preview_model"],
                                       one_pass=self.cfg["one_pass_stt"])
        self.spotter = WakeSpotter(self.mic, on_segment=lambda audio, n: on_main(self.on_wake_segment, audio, n),
                                   paused=lambda: self.listener.active,
                                   on_error=lambda err: on_main(self.on_wake_error, err), on_note=log)
        self.glow = Glow()
        self.build_menus()
        self.build_panel()
        self.build_bubble()
        self.register_hotkey()
        self.setup_key_watch()
        self.set_state("idle", "waking up")
        self.bubble_say("Starting up")
        self.brain = Brain(self.cfg, BrainUI(self))
        AppHelper.callLater(10, self.health_check)
        log(f"{self.name} started (pid {os.getpid()}, wake word {'on' if self.cfg['wake_word'] else 'off'})")
        self.transcriber.warm_up()
        if self.cfg["wake_word"]:
            self.transcriber.warm_up(fast=True)
            self.spotter.start()

    # ---------- building ----------

    def build_menus(self):
        # Hidden main menu: without it ⌘C, ⌘V and ⌘A don't work in the text box of a menu bar app.
        main, edit_item = NSMenu.alloc().init(), NSMenuItem.alloc().init()
        edit = NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in [("Undo", "undo:", "z"), ("Redo", "redo:", "Z"), ("Cut", "cut:", "x"),
                                   ("Copy", "copy:", "c"), ("Paste", "paste:", "v"), ("Select All", "selectAll:", "a")]:
            edit.addItemWithTitle_action_keyEquivalent_(title, action, key)
        edit_item.setSubmenu_(edit)
        main.addItem_(edit_item)
        NSApp.setMainMenu_(main)

        self.status_item = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        menu = NSMenu.alloc().init()

        def item(title, action, key=""):
            mi = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key)
            mi.setTarget_(self.target)
            menu.addItem_(mi)
            return mi

        talk = item(f"Talk to {self.name}", "talk:")
        key = self.cfg["hotkey"].split("+")[-1].strip().lower()
        talk.setKeyEquivalent_(" " if key == "space" else "\r" if key in ("return", "enter") else key)
        talk.setKeyEquivalentModifierMask_(self.menu_mods)
        item("Stop", "stop:")
        item("New conversation", "newChat:")
        menu.addItem_(NSMenuItem.separatorItem())
        self.speak_item = item("Speak replies", "toggleSpeak:")
        self.speak_item.setState_(1 if self.cfg["speak_replies"] else 0)
        self.wake_item = item(f"Listen for “Hey {self.name}”", "toggleWake:")
        self.wake_item.setState_(1 if self.cfg["wake_word"] else 0)
        self.follow_item = item("Keep listening after replies", "toggleFollow:")
        self.follow_item.setState_(1 if self.cfg["follow_up"] else 0)
        self.glow_item = item("Screen glow", "toggleGlow:")
        self.glow_item.setState_(1 if self.cfg["glow"] else 0)
        self.lock_item = item("Only listen to my voice", "toggleVoiceLock:")
        self.lock_item.setState_(1 if self.cfg["voice_lock"] and self.voiceid.enrolled else 0)
        item("Set up my voice…", "setupVoice:")
        self.ptt_item = item("Hold ⌥ Option to talk", "togglePushToTalk:")
        self.ptt_item.setState_(1 if self.cfg["push_to_talk"] else 0)
        item("Edit personality…", "editPersona:")
        item("Edit memory…", "editMemory:")
        item("Open log", "showLog:")
        menu.addItem_(NSMenuItem.separatorItem())
        item(f"Quit {self.name}", "quit:", "q")
        self.status_item.setMenu_(menu)

    def build_panel(self):
        p = Panel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, PANEL_W, PANEL_H), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        p.setLevel_(NSFloatingWindowLevel)
        p.setOpaque_(False)
        p.setBackgroundColor_(NSColor.clearColor())
        p.setHasShadow_(True)
        p.setHidesOnDeactivate_(False)
        p.setReleasedWhenClosed_(False)
        p.setCollectionBehavior_(NSWindowCollectionBehaviorMoveToActiveSpace | NSWindowCollectionBehaviorFullScreenAuxiliary)
        p.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
        p.setDelegate_(self.target)
        view = glass(NSMakeRect(0, 0, PANEL_W, PANEL_H), 18)
        p.setContentView_(view)

        conf = WKWebViewConfiguration.alloc().init()
        conf.userContentController().addScriptMessageHandler_name_(self.target, "companion")
        web = WKWebView.alloc().initWithFrame_configuration_(view.bounds(), conf)
        web.setValue_forKey_(False, "drawsBackground")
        web.setAutoresizingMask_(NSViewWidthSizable | NSViewHeightSizable)
        view.addSubview_(web)
        ui = HERE / "ui"
        web.loadFileURL_allowingReadAccessToURL_(NSURL.fileURLWithPath_(str(ui / "index.html")),
                                                 NSURL.fileURLWithPath_(str(ui)))
        self.panel, self.web = p, web

    def build_bubble(self):
        """Small click-through status line shown while the panel is hidden."""
        b = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 300, 40), NSWindowStyleMaskBorderless | NSWindowStyleMaskNonactivatingPanel,
            NSBackingStoreBuffered, False)
        b.setLevel_(NSStatusWindowLevel)
        b.setOpaque_(False)
        b.setBackgroundColor_(NSColor.clearColor())
        b.setHasShadow_(True)
        b.setIgnoresMouseEvents_(True)
        b.setHidesOnDeactivate_(False)
        b.setSharingType_(NSWindowSharingNone)  # keep it out of screenshots where macOS allows
        b.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
                                 | NSWindowCollectionBehaviorStationary | NSWindowCollectionBehaviorIgnoresCycle)
        b.setAppearance_(NSAppearance.appearanceNamed_("NSAppearanceNameDarkAqua"))
        b.setContentView_(glass(NSMakeRect(0, 0, 300, BUBBLE_H), BUBBLE_H / 2))
        dot = NSView.alloc().initWithFrame_(NSMakeRect(15, (BUBBLE_H - 7) / 2, 7, 7))
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(3.5)
        label = NSTextField.labelWithString_("")
        label.setFont_(NSFont.systemFontOfSize_weight_(13, 0.23))
        label.setTextColor_(NSColor.colorWithWhite_alpha_(1.0, 0.92))
        label.setLineBreakMode_(4)  # truncate tail
        b.contentView().addSubview_(dot)
        b.contentView().addSubview_(label)
        self.bubble, self.bubble_label, self.bubble_dot = b, label, dot

    def register_hotkey(self):
        @quickHotKey(virtualKey=self.vk, modifierMask=self.mods)
        def pressed():
            on_main(self.hotkey)
        self._hotkey = pressed
        # `kill -USR1 <pid>` (see talk.sh) acts like the hotkey, for Hammerspoon, Shortcuts and friends.
        MachSignals.signal(signal.SIGUSR1, lambda signum: on_main(self.hotkey))

    # ---------- small helpers ----------

    def js(self, fn, *args):
        code = f"ui.{fn}({', '.join(json.dumps(a) for a in args)})"
        if not self.js_ready:
            self.js_queue.append(code)
        else:
            self.web.evaluateJavaScript_completionHandler_(code, None)

    def set_state(self, state, text=None):
        if DEBUG and state != self.state:
            log(f"state: {state}")
        self.state = state
        if self.cfg["glow"]:
            mode = {"listening": "listening", "transcribing": "thinking", "approval": "approval",
                    "working": "working" if self.hands_active else "thinking"}.get(state)
            self.glow.show(mode) if mode else self.glow.hide()
        if state == "idle" and self.bubble.isVisible() and not self.bubble_timed:
            gen = self.bubble_gen  # nothing else will replace it now, so it mustn't stay up forever
            AppHelper.callLater(4, lambda: gen == self.bubble_gen and self.bubble.orderOut_(None))
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(ICONS[state], self.name)
        if img:
            img.setTemplate_(True)
            self.status_item.button().setImage_(img)
        else:
            self.status_item.button().setTitle_("✦")
        self.js("setState", state, text)

    def bubble_say(self, text, hide_after=None):
        self.bubble_gen += 1
        self.bubble_timed = bool(hide_after)
        if self.panel.isVisible():
            self.bubble.orderOut_(None)
            return
        self.bubble_label.setStringValue_(" ".join(text.split()))
        self.bubble_label.sizeToFit()
        r, g, b_ = STATE_COLORS.get(self.state, STATE_COLORS["idle"])  # the dot shows what Mac is doing
        self.bubble_dot.layer().setBackgroundColor_(NSColor.colorWithSRGBRed_green_blue_alpha_(r, g, b_, 1).CGColor())
        lh = self.bubble_label.frame().size.height
        w, h = min(560, self.bubble_label.frame().size.width + 48), BUBBLE_H
        vf = mouse_screen().visibleFrame()
        self.bubble.setFrame_display_(NSMakeRect(vf.origin.x + (vf.size.width - w) / 2, vf.origin.y + 24, w, h), True)
        self.bubble_label.setFrame_(NSMakeRect(30, (h - lh) / 2, w - 48, lh))
        self.bubble.orderFrontRegardless()
        if hide_after:
            gen = self.bubble_gen
            AppHelper.callLater(hide_after, lambda: gen == self.bubble_gen and self.bubble.orderOut_(None))

    def show_panel(self):
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front and front.processIdentifier() != os.getpid():
            self.prev_app = front
        if not self.panel.isVisible():
            vf = mouse_screen().visibleFrame()
            x = vf.origin.x + (vf.size.width - PANEL_W) / 2
            y = vf.origin.y + vf.size.height * 0.9 - PANEL_H
            self.panel.setFrame_display_(NSMakeRect(x, y, PANEL_W, PANEL_H), True)
        self.bubble.orderOut_(None)
        self.panel.orderFrontRegardless()
        self.panel.makeKeyWindow()
        self.panel.makeFirstResponder_(self.web)
        self.js("focus")

    def hide_panel(self, restore=True):
        if not self.panel.isVisible():
            return
        self.panel.orderOut_(None)
        if restore:
            self.restore_focus()
        if self.state == "working":
            self.bubble.orderOut_(None)  # the screen glow shows Mac is working; no step-by-step text

    def restore_focus(self):
        """Make sure the app you were in has the keyboard, so the companion's typing lands there.
        The panel never activates us, so this only matters if something else did (like a click on our menu)."""
        app = self.prev_app
        if not NSApp.isActive():
            return
        if app is None or app.isTerminated():
            NSApp.deactivate()
            return
        if hasattr(NSApp, "yieldActivationToApplication_"):
            NSApp.yieldActivationToApplication_(app)
        app.activateWithOptions_(0)

    # ---------- your input ----------

    def hotkey(self):
        if self.state == "listening":
            self.listener.stop()
        elif self.state == "transcribing":
            pass
        elif self.state == "working":  # you cut in: stop the task, then listen for what you want instead
            self.cut_in("you pressed the hotkey")
            self.show_panel()
            self.start_listening()
        elif self.panel.isVisible() and self.panel.isKeyWindow() and self.state == "idle":
            self.hide_panel()
        else:
            self.show_panel()
            if self.state in ("idle", "approval"):
                self.start_listening()

    def start_listening(self, seed=None, since=None, deaf=False, follow=False, hold=False):
        if self.listener.active:
            return
        self.following = follow
        self.held = hold
        self.speaker.stop()
        self.set_state("listening", "Listening for your reply" if follow else None)
        self.early.reset()
        self.listener.start(seed=seed, since=since, deaf=deaf, hold=hold)
        if self.cfg["keep_warm"] and self.enroll is None:
            self.brain.warm()  # while you talk; does nothing if Jev and Claude were used recently
        self.preview_gen += 1
        if self.enroll is None:
            AppHelper.callLater(0.9, self.preview_tick, self.preview_gen)

    def preview_tick(self, gen):
        """While you talk, show what Mac has heard so far, about once a second. With early_final, also start the final
        transcription at the first short pause (checked every 0.1 s, so a pause isn't missed)."""
        if gen != self.preview_gen or self.state != "listening" or not self.listener.active:
            return
        early, now = self.cfg["early_final"], time.monotonic()
        # Skip once you pause: the final words are about to arrive and shouldn't wait behind a preview.
        if (self.listener.voiced >= 5 and self.listener.quiet_for < (EARLY_S if early else 0.35)
                and not self.preview_busy and now >= self.preview_due):
            audio = self.listener.snapshot()
            if audio is not None:
                self.preview_busy, self.preview_due = True, now + 0.8
                self.transcriber.preview(audio, lambda text: on_main(self.on_preview, gen, text))
        if early:
            self.start_early()
        AppHelper.callLater(0.1 if early else 0.8, self.preview_tick, gen)

    def start_early(self):
        """At a short pause, transcribe what you've said so far. If you don't speak again, that's the final text.
        One job at a time: a second would only queue behind the first."""
        n = self.listener.voiced  # before the audio: speech that comes in between makes the result unusable
        if n < 5 or not EARLY_S <= self.listener.quiet_for < END_SILENCE_S or n == self.early.voiced or self.early.running:
            return
        audio = self.listener.snapshot()
        if audio is not None:
            self.early.start(n)
            self.transcriber.transcribe(audio, lambda text, err: on_main(self.on_early, n, text, err),
                                        context=self.whisper_context(), stale=lambda: self.listener.voiced != n)

    def on_early(self, voiced, text, error):
        if self.early.voiced == voiced:  # not a late answer for an older stretch of speech
            if self.cfg["early_jev"] and text and not error and not self.approval_id:
                self.ask_jev_early(text)  # before result(): that may send the request at once
            self.early.result((text, error))

    def ask_jev_early(self, text):
        """early_jev: start Jev on the early text the way on_transcribed would send it, so if you say nothing more
        its answer is ready when listening ends."""
        rest = wake_rest(text, self.wake_names)
        text = text if rest is None else rest
        if text and not is_stop(text):
            lang = self.transcriber.last_language
            self.brain.prefetch(text, self.following and lang == "en", lang)

    def whisper_context(self):
        """Right after Mac spoke, what it said, so your answer is spelled the same way."""
        return self.brain.last_reply if time.monotonic() - self.brain.last_active < 180 else ""

    def on_preview(self, gen, text):
        self.preview_busy = False
        if gen != self.preview_gen or self.state != "listening" or not text:
            return
        rest = wake_rest(text, self.wake_names)
        self.show_draft(text if rest is None else rest)

    def show_draft(self, text):
        """Your words so far: a faded message in the panel, or the bubble when the panel is closed."""
        if not text:
            return
        self.js("draft", text)
        if not self.panel.isVisible():
            self.bubble_say(text if len(text) <= 64 else "…" + text[-63:])

    def clear_draft(self):
        self.js("draft", "")
        self.bubble.orderOut_(None)

    def follow_up(self):
        """After a spoken reply, listen for your answer without needing the wake word."""
        if self.state != "idle" or self.listener.active:
            return
        if not self.panel.isVisible():
            self.bubble_say("Listening for your reply", hide_after=7)
        log("listening for your reply")
        self.start_listening(deaf=True, follow=True)

    def on_heard(self, audio, error, speech_end=None, listen_end=None):
        """speech_end, listen_end: when you stopped talking and when the Listener ended (time.monotonic())."""
        if self.state != "listening":  # answered or stopped some other way meanwhile
            return
        self.preview_gen += 1
        if self.enroll is not None:
            self.enroll_heard(audio)
            return
        if audio is not None and self.following and not self.is_you(audio, "a reply"):
            audio = None  # someone else talking after Mac spoke: the conversation is over
        if audio is not None and self.held and self.cfg["voice_adapt"] and self.voiceid.enrolled:
            threading.Thread(target=self.learn_voice, args=(audio,), daemon=True).start()
        back = "approval" if self.approval_id else "idle"
        if error:
            self.set_state(back)
            self.js("add", "error", error)
        elif audio is None:
            # Nothing said (or you didn't reply after an answer, which ends the conversation).
            log("voice: nothing heard" + (" (conversation over)" if self.following else ""))
            self.set_state(back)
            self.clear_draft()
            if not self.following:
                self.js("hint", "Didn't catch anything")
        else:
            ended = listen_end or time.monotonic()
            self.t_heard = (speech_end or ended, ended)
            self.set_state("transcribing")  # the live words (if any) stay up until the final words replace them
            if self.cfg["early_final"] and self.early.usable(self.listener.voiced):
                self.early.when_ready(lambda result: self.on_transcribed(*result))  # nothing said since it started
                return
            self.transcriber.transcribe(audio, lambda text, err: on_main(self.on_transcribed, text, err),
                                        context=self.whisper_context())

    def on_transcribed(self, text, error):
        if self.state != "transcribing":
            return
        rest = wake_rest(text, self.wake_names)
        if rest is not None:
            text = rest  # "Hey Mac, open Spotify" -> "open Spotify"
        if self.approval_id:
            self.clear_draft()
            self.set_state("approval")
            if NO.search(text):
                self.js("answered", False)
            elif YES.search(text):
                self.js("answered", True)
            else:
                self.js("hint", 'Say "yes" or "no"')
            return
        self.set_state("idle")
        if error:
            self.js("add", "error", error)
        elif not text:
            log("voice: nothing usable in what was heard (noise, or a filtered mishearing)")
            self.clear_draft()
            if self.following:
                self.bubble.orderOut_(None)
            else:
                self.js("hint", "I'm here, but didn't catch that")
        elif is_stop(text):
            self.clear_draft()
            self.stop_everything(text)
        elif re.search(r"\b(set ?up|learn|train|teach|record|reset)\b.*\bmy voice\b", text, re.I):
            self.start_voice_setup()
        else:
            lang = self.transcriber.last_language
            if not self.panel.isVisible():
                self.show_draft(text)  # your final words stay in the bubble until the answer comes
            self.submit(text, reply_to_last=self.following and lang == "en", lang=lang, t_heard=self.t_heard)

    def submit(self, text, reply_to_last=False, lang=None, t_heard=None):
        """t_heard: when a spoken request was heard (see on_heard); None when typed."""
        lang = lang or script_language(text)  # typed text: judge by the alphabet
        if self.stopping:  # you cut in with a new request: run it as soon as the old task has stopped
            self.pending_request = (text, lang, t_heard)
            self.set_state("working", "stopping the last thing...")
            return
        if self.state in ("working", "approval") or self.brain.busy:
            log("voice: request dropped, Mac was busy")
            self.js("hint", "Still on the last thing")
            return
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front and front.processIdentifier() != os.getpid():
            self.prev_app = front  # in a conversation the app you're in may have changed since the panel opened
        self.js("add", "user", text)
        self.last_activity = None
        self.set_state("working")
        # Set only once accepted: a request dropped above must not change the one the brain is on.
        self.request_lang = lang
        self.voice_turn = t_heard is not None
        self.t_asked, self.t_submit, self.had_approval = t_heard, time.monotonic(), False
        self.streamer, self.spoke_stream, self.stream_chars = SentenceStreamer(), False, 0
        self.stream_t = t_heard[0] if t_heard else None
        self.brain.send(text, self.prev_app.localizedName() if self.prev_app else "", reply_to_last, lang)

    def on_js(self, m):
        t = m.get("type")
        if t == "ready":
            self.js_ready = True
            self.js("init", {"name": self.name, "user": self.cfg["user_name"], "hotkey": self.hotkey_label,
                             "wake": f"Hey {self.name}" if self.cfg["wake_word"] else "",
                             "ptt": bool(self.cfg["push_to_talk"])})
            for code in self.js_queue:
                self.web.evaluateJavaScript_completionHandler_(code, None)
            self.js_queue = []
        elif t == "send":
            self.submit(m["text"])
        elif t == "mic":
            if self.state == "listening":
                self.listener.stop()
            elif self.state in ("idle", "approval"):
                self.start_listening()
        elif t == "typing" and self.state == "listening":
            self.listener.cancel()
        elif t == "answer":
            if DEBUG:
                log(f"approval {m['id']} answered {'yes' if m['ok'] else 'no'} by {m.get('via')}")
            self.on_answer(m["id"], m["ok"])
        elif t == "stop":
            self.stop()
        elif t == "hide":
            self.cancel_voice_setup()
            self.hide_panel()
        elif t == "newchat":
            self.new_chat()

    def on_answer(self, rid, ok):
        if rid != self.approval_id:
            return
        self.approval_id = None
        self.speaker.stop()
        self.listener.cancel()
        self.set_state("working")
        self.last_activity = "Okay, going ahead" if ok else "Okay, I won't"
        self.hide_panel()
        # Let focus settle back on your app before the approved action runs.
        AppHelper.callLater(0.4, self.brain.answer, rid, ok)

    def on_panel_resigned(self):
        if self.state == "idle" and self.panel.isVisible():
            self.panel.orderOut_(None)

    def stop(self):
        self.listener.cancel()
        self.speaker.stop()
        if self.state in ("working", "approval") and not self.stopping:
            self.stopping = True
            self.brain.stop()
            self.set_state("working", "stopping...")
            AppHelper.callLater(15, self.stop_timed_out)

    def stop_timed_out(self):
        """Safety net: never stay stuck on "stopping" if the brain doesn't report back."""
        if self.stopping and not self.brain.busy:
            log("stop never finished; resetting")
            self.on_turn_done("", None, True)

    def stop_everything(self, text):
        """You said stop: end the task, the talking and the conversation (no listening for a reply).
        "Stop listening" / "go to sleep" / "mic off" also turns off the wake word, which closes the mic completely."""
        self.following = False
        self.stop()
        mic_off = bool(MIC_OFF.search(text)) and self.cfg["wake_word"]
        if mic_off:
            self.set_wake(False)
        log("you said stop" + (", mic off" if mic_off else ""))
        note = "Mic off" if mic_off else "Stopped"
        self.js("status", note)
        self.bubble_say(note, hide_after=4)
        if self.cfg["speak_replies"]:
            self.speaker.say("Okay, mic off." if mic_off else "Okay.")

    def cut_in(self, reason, note="Stopped"):
        """You interrupted while Mac was working: stop the task right away."""
        if self.state != "working" or self.stopping:
            return
        log(f"stopped because {reason}")
        self.stop_note = note
        self.stop()

    def new_chat(self):
        self.stop()
        self.brain.reset()
        self.js("clear")
        self.set_state("idle", "fresh start")

    def toggle_speak(self, sender):
        self.cfg["speak_replies"] = not self.cfg["speak_replies"]
        sender.setState_(1 if self.cfg["speak_replies"] else 0)
        if not self.cfg["speak_replies"]:
            self.speaker.stop()
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")

    def toggle_wake(self, sender):
        self.set_wake(not self.cfg["wake_word"])

    def set_wake(self, on):
        """Turn the always-listening wake word on or off. Off closes the mic whenever you're not talking to Mac."""
        self.cfg["wake_word"] = on
        self.wake_item.setState_(1 if on else 0)
        if on:
            self.spotter.start()
        else:
            self.spotter.stop()
        self.js("setWake", f"Hey {self.name}" if on else "")
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")

    def toggle_follow(self, sender):
        self.cfg["follow_up"] = not self.cfg["follow_up"]
        sender.setState_(1 if self.cfg["follow_up"] else 0)
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")

    # ---------- hold Option to talk ----------

    def setup_key_watch(self):
        """Watch the keyboard and mouse for two things: holding Option to talk, and you taking over while Mac is
        using the screen (which stops it). Watching other apps needs Accessibility permission (Terminal has it)."""
        if not AXIsProcessTrusted():
            log("key watch is off: Mac (or Terminal, when run from start.sh) needs Accessibility permission")
            self.js("add", "error", "Hold ⌥ to talk, and stopping when you take over, need Accessibility permission "
                                    "for Terminal: System Settings > Privacy & Security > Accessibility.")
            return
        mask = (NSEventMaskFlagsChanged | NSEventMaskKeyDown | NSEventMaskLeftMouseDown | NSEventMaskRightMouseDown
                | NSEventMaskOtherMouseDown | NSEventMaskMouseMoved | NSEventMaskLeftMouseDragged | NSEventMaskScrollWheel)
        self._key_monitors = [
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(mask, lambda e: self.on_key_event(e, local=False)),
            NSEvent.addLocalMonitorForEventsMatchingMask_handler_(mask, lambda e: (self.on_key_event(e, local=True), e)[1]),
        ]

    @staticmethod
    def by_you(event):
        """Your real keyboard or mouse. Mac's own clicks and typing come from the cliclick tool, and every
        program-made event carries that program's process ID; real hardware events carry 0."""
        try:
            return Quartz.CGEventGetIntegerValueField(event.CGEvent(), Quartz.kCGEventSourceUnixProcessID) == 0
        except Exception:
            return False

    def on_key_event(self, event, local):
        try:
            kind = event.type()
            # You took over while Mac was using the screen: stop. (Clicks in Mac's own panel don't count.)
            if (not local and self.state == "working" and self.hands_active and not self.stopping
                    and kind != NSEventTypeFlagsChanged and self.by_you(event)):
                if kind in (NSEventTypeMouseMoved, NSEventTypeLeftMouseDragged):
                    self.user_moved += abs(event.deltaX()) + abs(event.deltaY())
                    if self.user_moved > 40:  # a real move, not a bump of the desk
                        self.cut_in("you moved the mouse", "Stopped, it's all yours")
                else:
                    self.cut_in("you clicked, scrolled or typed", "Stopped, it's all yours")
            if kind in (NSEventTypeMouseMoved, NSEventTypeLeftMouseDragged) or not self.cfg["push_to_talk"]:
                return
            if kind == NSEventTypeFlagsChanged:
                flags = event.modifierFlags()
                side = OPTION_SIDE.get(self.cfg["push_to_talk"])
                option = bool(flags & NSEventModifierFlagOption) and (side is None or bool(flags & side))
                others = bool(flags & (NSEventModifierFlagCommand | NSEventModifierFlagControl | NSEventModifierFlagShift))
                if option and not others and not self.ptt_down:
                    self.ptt_down = True
                    self.ptt_gen += 1
                    # With the mic already open, keep your words from the moment you pressed, not after the hold delay.
                    self.ptt_since = self.mic.count if self.mic.is_open else None
                    AppHelper.callLater(HOLD_S, self.ptt_begin, self.ptt_gen)
                elif self.ptt_down and (not option or others):
                    self.ptt_end(send=not others)
            elif self.ptt_down:
                self.ptt_end(send=False)  # a key or a click with Option held is a shortcut, not talking
        except Exception:
            log("hold-to-talk error:\n" + traceback.format_exc())

    def ptt_begin(self, gen):
        if gen != self.ptt_gen or not self.ptt_down:
            return  # let go, or used a shortcut, before the hold counted
        if self.listener.active:  # already listening (like right after a reply): letting go sends it
            self.ptt_talking = True
            self.play("start")
            return
        if self.state == "working":
            self.cut_in("you held ⌥ to talk")  # stop the task; what you say next is the new request
        elif self.state not in ("idle", "approval"):
            log(f"hold-to-talk: ignored ({self.state})")
            return
        log("hold-to-talk: listening")
        self.play("start")
        self.ptt_talking = True
        if not self.panel.isVisible():
            self.bubble_say("Listening")
        self.start_listening(since=self.ptt_since, hold=True)

    def ptt_end(self, send):
        self.ptt_down = False
        self.ptt_gen += 1
        if self.ptt_talking:
            self.ptt_talking = False
            log("hold-to-talk: " + ("let go, sending" if send else "cancelled (a key, click or other modifier while ⌥ was held)"))
            if send:
                self.play("stop")
                self.listener.stop()
            else:
                self.listener.cancel()  # used ⌥ as a shortcut: no sound

    def toggle_push_to_talk(self, sender):
        self.cfg["push_to_talk"] = "" if self.cfg["push_to_talk"] else self.ptt_choice
        sender.setState_(1 if self.cfg["push_to_talk"] else 0)
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")

    def health_check(self):
        """Speech recognition normally takes under a second. If it's been stuck far longer (it happened once,
        cause unknown), everything that listens is stuck behind it, so restart; start.sh's watchdog brings Mac back."""
        busy = self.transcriber.busy_since
        if busy is not None and time.monotonic() - busy > 25:
            log("speech recognition got stuck, restarting Mac")
            os._exit(75)  # non-zero: the watchdog restarts it in 3 s
        AppHelper.callLater(10, self.health_check)

    # ---------- only your voice ----------

    def is_you(self, audio, what):
        """With "Only listen to my voice" on, is this your voice? (Holding ⌥ or the hotkey skips this: you pressed it.)"""
        if not self.cfg["voice_lock"] or not self.voiceid.enrolled:
            return True
        ok, sim = self.voiceid.check(audio)
        log(f"voice lock: {what} " + (f"matched your voice ({sim:.2f})" if ok else f"from another voice, ignored ({sim:.2f})"))
        return ok

    def learn_voice(self, audio):
        """A hold-to-talk request is certainly you, so the voiceprint learns from it (voice_adapt)."""
        sim, updated = self.voiceid.adapt(audio)
        if sim is not None:
            log(f"voice lock: {'learned from' if updated else 'too unlike your voiceprint to learn from'} hold-to-talk ({sim:.2f})")

    def toggle_glow(self, sender):
        self.cfg["glow"] = not self.cfg["glow"]
        sender.setState_(1 if self.cfg["glow"] else 0)
        if not self.cfg["glow"]:
            self.glow.hide()
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")

    def toggle_voice_lock(self, sender):
        if not self.voiceid.enrolled:
            self.start_voice_setup()  # nothing to compare with yet
            return
        self.cfg["voice_lock"] = not self.cfg["voice_lock"]
        sender.setState_(1 if self.cfg["voice_lock"] else 0)
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")

    def start_voice_setup(self):
        """Read 4 sentences out loud; Mac makes a voiceprint from them."""
        if self.state not in ("idle", "listening") or self.enroll is not None:
            return
        self.listener.cancel()
        self.enroll = {"i": 0, "clips": []}
        log("voice setup started")
        self.show_panel()
        self.js("enroll", 1, len(SENTENCES), SENTENCES[0], "")
        intro = "Let's learn your voice. Read each sentence on the screen out loud."
        self.speaker.say(intro, then=lambda: on_main(self.enroll_listen))

    def enroll_listen(self):
        if self.enroll is not None and not self.listener.active:
            self.set_state("idle")
            self.start_listening()

    def enroll_heard(self, audio):
        e = self.enroll
        self.set_state("idle")
        if audio is None or len(audio) < 1.5 * 16000:
            self.js("enroll", e["i"] + 1, len(SENTENCES), SENTENCES[e["i"]], "Didn't catch that. Please read it again.")
        else:
            e["clips"].append(audio)
            e["i"] += 1
            if e["i"] == len(SENTENCES):
                self.finish_voice_setup()
                return
            self.js("enroll", e["i"] + 1, len(SENTENCES), SENTENCES[e["i"]], "")
        AppHelper.callLater(0.4, self.enroll_listen)

    def finish_voice_setup(self):
        clips, self.enroll = self.enroll["clips"], None
        scores = self.voiceid.enroll(clips)
        log("voice setup done, sentence match: " + ", ".join(f"{s:.2f}" for s in scores))
        self.cfg["voice_lock"] = True
        self.lock_item.setState_(1)
        CONFIG.write_text(json.dumps(self.cfg, indent=2) + "\n")
        self.js("enrollDone", "All set. From now on I only answer to your voice.")
        if self.cfg["speak_replies"]:
            self.speaker.say("All set. From now on I only answer to your voice.")

    def cancel_voice_setup(self):
        if self.enroll is not None:
            self.enroll = None
            self.listener.cancel()
            self.js("enrollDone", "Voice setup cancelled. Nothing was changed.")
            log("voice setup cancelled")

    # ---------- the wake word ----------

    def on_wake_segment(self, audio, n):
        """Someone said something. Check it with the small fast model."""
        if self.listener.active:
            return
        if self.wake_checking:  # busy with the previous burst: check this one next instead of dropping it
            self.wake_pending = (audio, n)
            return
        self.wake_checking = True
        self.transcriber.transcribe(audio, lambda text, err: on_main(self.on_wake_check, audio, n, text, False), fast=True)

    def on_wake_check(self, audio, n, text, accurate):
        if DEBUG:
            log(f"heard {len(audio) / 16000:.1f}s ({'accurate' if accurate else 'fast'}): {text!r}")
        rest = wake_rest(text, self.wake_names)
        if rest is None and not accurate and (mentions(text, self.wake_names) or len(text.split()) <= 5):
            # The small model mishears short phrases ("Hey Mac" -> "He may say") and can garble a sentence's start.
            # Take a careful second listen with the big model.
            self.transcriber.transcribe(audio, lambda t, err: on_main(self.on_wake_check, audio, n, t, True), hint=False)
            return
        self.wake_checking = False
        if rest is not None and not self.is_you(audio, "the wake word"):
            rest = None  # someone else (or the TV) said "Hey Mac"
        if rest is not None:
            self.wake_pending = None
            log(f"wake word: {text!r}")
            self.on_wake(audio, n, rest)
        elif self.wake_pending:
            audio, n = self.wake_pending
            self.wake_pending = None
            self.on_wake_segment(audio, n)

    def on_wake(self, audio, n, rest):
        self.speaker.stop()
        if rest and is_stop(rest):  # "Hey Mac, stop" / "Hey Mac, stop listening"
            self.stop_everything(rest)
            return
        if self.state == "working":
            self.cut_in("you said the wake word")  # stop the task; the rest of what you said is the new request
        elif self.state not in ("idle", "approval"):
            return
        if self.listener.active:
            return
        self.show_panel()
        if len(rest.split()) >= 2 or (self.approval_id and rest):
            # The request (or the yes/no) came in the same breath: keep listening in case you're not done.
            self.start_listening(seed=audio, since=n)
        else:
            self.chime()
            self.start_listening(since=n, deaf=True)

    def on_wake_error(self, error):
        self.js("add", "error", f"The wake word can't use the microphone: {error}")

    def voice_for(self, text):
        """Tamil replies need a Tamil voice. There's no Malayalam voice, so Malayalam replies come in Manglish
        (Malayalam in English letters), read by an Indian English voice."""
        lang = script_language(text)
        if lang == "en" and getattr(self, "request_lang", "en") == "ml":
            lang = "ml"
        return self.cfg["voices"].get(lang) if lang != "en" else None

    def play(self, name):
        """Hold-to-talk cues: sounds/start.wav (rising: listening) and sounds/stop.wav (falling: sending)."""
        if not self.cfg["ptt_sounds"]:
            return
        if not hasattr(self, "_sounds"):
            self._sounds = {}
        sound = self._sounds.get(name)
        if sound is None:
            sound = NSSound.alloc().initWithContentsOfFile_byReference_(str(HERE / "sounds" / f"{name}.wav"), True)
            if sound is None:
                return
            sound.setVolume_(0.6)
            self._sounds[name] = sound
        sound.stop()
        sound.play()

    def chime(self):
        self._chime = NSSound.soundNamed_("Tink")
        if self._chime:
            self._chime.setVolume_(0.4)
            self._chime.play()

    def listen_for_answer(self, rid):
        """Hands-free approvals: after asking out loud, listen for your yes or no."""
        if self.approval_id == rid and not self.listener.active:
            self.start_listening()

    def shutdown(self):
        self.speaker.stop()
        self.brain.shutdown()

    # ---------- the brain's side ----------

    def on_connected(self, error):
        if error:
            self.set_state("idle", "can't reach Claude")
            self.js("add", "error", error)
            self.bubble_say(error, hide_after=8)
        else:
            if self.state == "idle":
                self.set_state("idle")
            self.bubble_say(f"{self.name} is ready", hide_after=4)

    def on_level(self, x):
        self.js("level", x)
        if self.cfg["glow"]:
            self.glow.level(x)

    def on_activity(self, text):
        """Mac's steps ("clicking", "running ...") aren't shown: the orb and the screen glow say it's working.
        Only the kill switch is worth words, so you know why it stopped."""
        self.last_activity = text
        if text.startswith("Kill switch"):
            self.js("status", text)
            self.bubble_say(text, hide_after=4)

    def on_assistant_text(self, text):
        self.js("add", "assistant", text)
        self.last_activity = text
        if not self.panel.isVisible():
            self.bubble_say(text)

    def on_approval(self, rid, title, detail):
        self.approval_id, self.had_approval = rid, True
        self.set_state("approval")
        self.show_panel()
        self.js("approval", rid, title, detail)
        if self.cfg["speak_replies"]:
            then = (lambda: on_main(self.listen_for_answer, rid)) if self.cfg["wake_word"] else None
            self.speaker.say(f"Quick check. {title} Yes or no?", then=then)

    def streams(self):
        return self.cfg["stream_speech"] and self.cfg["speak_replies"] and not self.stopping

    def on_reply_delta(self, text):
        """Claude's reply as it is written: each sentence is spoken as soon as it's complete."""
        if self.streams():
            self.speak_stream(self.streamer.feed(text))

    def on_reply_end(self):
        """The reply is complete: speak the last sentence now, before the turn finishes."""
        if self.streams():
            self.speak_stream(self.streamer.flush())

    def on_reply_discard(self):
        """The text so far came before a tool call (narration): don't speak it. What is already being said goes on."""
        self.streamer = SentenceStreamer()
        if self.spoke_stream and (t := self.speaker.drop_pending()) is not None:
            self.stream_t = t  # it hadn't started: the next sentence logs the first speech

    def speak_stream(self, chunks):
        for chunk in chunks:
            if self.stream_chars >= SAY_MAX:
                return  # a long reply is cut off, as a whole one was
            t, self.stream_t = self.stream_t, None  # only the first sentence spoken carries it
            self.spoke_stream = True
            self.stream_chars += len(speakable(chunk))
            # After an approval the time would include how long you took to answer.
            self.speaker.enqueue(chunk, voice=self.voice_for(chunk), t_heard=None if self.had_approval else t)

    def on_clear_screen(self, done):
        """Mac is about to use the screen. From now on your mouse or keys mean "stop"."""
        self.hands_active, self.user_moved = True, 0.0
        if self.cfg["glow"] and self.state == "working":
            self.glow.show("working")  # a calm frame: Mac is using your screen
        if not self.panel.isVisible() or self.state == "approval":
            done()
        else:
            self.hide_panel()
            AppHelper.callLater(0.4, done)

    def on_ignored(self, text):
        """What was heard after Mac spoke was background talk, not for Mac: drop it and stop listening."""
        log("ignored background talk")
        self.js("status", "(ignored background talk)")
        self.bubble.orderOut_(None)
        if self.state == "working":
            self.set_state("idle")

    def on_turn_done(self, final, error, interrupted):
        self.stopping = self.hands_active = False
        line = answer_timing(self.t_asked, self.t_submit, time.monotonic(), error, interrupted, self.had_approval)
        if line:
            log(line)
        # For the "first speech" line when the reply starts; after an approval it would include your answer.
        spoke = self.t_asked[0] if line and not self.had_approval else None
        self.t_asked = self.t_submit = None  # at most one line per request
        if self.approval_id:
            self.js("cancelCard")
            self.approval_id = None
        if self.state not in ("listening", "transcribing"):  # you may already be talking after cutting in
            self.set_state("idle")
        if error:
            self.js("add", "error", error)
            self.bubble_say("Something went wrong", hide_after=6)
            if self.cfg["speak_replies"]:
                self.speaker.say("Sorry, something went wrong.")
        elif interrupted:
            self.js("status", self.stop_note)
            if not self.listener.active and not self.pending_request:
                self.bubble_say(self.stop_note, hide_after=3)
            self.stop_note = "Stopped"
        else:
            # Listen for your reply only after Mac actually said something (not after a silent "opened Spotify").
            follow = self.cfg["follow_up"] and self.voice_turn and bool(final)
            then = (lambda: on_main(self.follow_up)) if follow else None
            if self.spoke_stream and self.cfg["speak_replies"]:
                self.speak_stream(self.streamer.flush())  # usually nothing left: on_reply_end took it
                if then:
                    self.speaker.enqueue("", then=then)  # once the last sentence is said
            elif final and self.cfg["speak_replies"]:
                self.speaker.say(final, then=then, voice=self.voice_for(final), t_heard=spoke)
            elif follow:
                AppHelper.callLater(0.3, self.follow_up)
            if final:
                self.bubble_say(final, hide_after=max(4, len(final) / 14))
            else:
                self.bubble.orderOut_(None)
        if self.pending_request:  # what you asked for when you cut in
            (text, lang, t_heard), self.pending_request = self.pending_request, None
            self.submit(text, lang=lang, t_heard=t_heard)


def main():
    lock = open(HERE / ".lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Companion is already running.")
        sys.exit(0)  # not an error, so start.sh's watchdog doesn't keep retrying
    lock.write(str(os.getpid()))
    lock.flush()
    # Record anything that goes wrong, so an unexpected stop is never a mystery.
    sys.excepthook = lambda *exc: log("crash:\n" + "".join(traceback.format_exception(*exc)))
    threading.excepthook = lambda a: log(f"error in thread {a.thread.name}:\n"
                                         + "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    target = Target.alloc().init()
    app.setDelegate_(target)
    AppHelper.runEventLoop()


if __name__ == "__main__":
    main()
