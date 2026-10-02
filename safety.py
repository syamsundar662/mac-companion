"""What needs your yes. Mac asks only before something is deleted or overwritten and goes ahead with everything else
(sending, buying, submitting forms, quitting, settings). Jev decides (router.judge) from the action described in words:
for clicks and keys, what's under the pointer or has the focus, read through ax.py. If Jev can't answer, the local
rules below decide. Claude never does: brain.py runs every action past check() first. Facts Jev can't see are added
to what it reads (a script's text) or decide without it (what's on disk). Secret-looking values never reach Jev."""
import asyncio
import os
import re
import shlex
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import ax
from router import took

# Ask when p(deletes) + p(overwrites) is at least this. Live (tests/test_jev_safety_live.py, 3 runs): risky 0.85 to
# 1.00, safe 0.00 to 0.34 (return on Save in a new file's save sheet: 0.45 to 0.54); the middle of that gap, so a
# run's noise (up to 0.08) can't flip either side.
RISK_MIN = 0.6
AX_TIMEOUT_S = 2.0    # reading the screen: a hung app can't hold a check longer
RETRY_S = 0.2         # an unlabelled click target is read once more after this (Chrome labels a moment after the wake)
CACHE_MAX = 500
SCRIPT_CHARS = 2000   # of a script's text, added to what Jev reads
UNCHECKED = {"screenshot", "scroll", "move", "frontmost", "displays"}  # mac tools that can't change anything
PRESSES = ("return", "space")  # keys that press the default or focused button
RETURNS = {"ctrl+m", "ctrl+j"}  # a terminal reads these as return too (and any return: shift+return, enter)
RUNNABLE = (".command", ".sh", ".tool", ".py", ".scpt")  # files open_app would run rather than show
SYSTEM_APPS = ("/Applications/", "/System/")
INTERPRETER = re.compile(r"(ba|z|da|k|fi)?sh|python[\d.]*|node|osascript|perl|ruby|source|\.")
# Words in front of a command, with their flags that take a value, and how many words follow them (timeout's 30s):
PREFIXES = {"sudo": ({"-u", "-g", "-U", "-C", "-D", "-h", "-p", "-r", "-t", "-T"}, 0), "env": ({"-u", "-P", "-S", "-C"}, 0),
            "exec": ({"-a"}, 0), "nohup": (set(), 0), "time": (set(), 0), "command": (set(), 0), "nice": ({"-n"}, 0),
            "caffeinate": ({"-t", "-w"}, 0), "timeout": ({"-s", "-k", "--signal", "--kill-after"}, 1)}
COPIES = ("cp", "mv", "ditto", "install")  # onto something that exists: replaced
WRITES = {">", ">|", "&>", ">&"}  # redirects that replace a file (">&" only when the target isn't 1, 2 or -)
OPERATOR, SEPARATOR = set(";&|<>()\n"), set(";&|()\n")  # shell punctuation; the kind that ends a command
HEREDOC = re.compile(r"(?<!<)<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")  # <<EOF, <<'EOF', <<-EOF (not <<< text)
MAY_REPLACE = "It may replace an existing file."
DEBUG = bool(os.environ.get("COMPANION_DEBUG"))  # labels and commands go in the log only in debug mode
TERMINALS = {"terminal", "iterm", "iterm2", "warp", "ghostty", "alacritty", "kitty", "wezterm", "wezterm-gui", "hyper",
             "tabby"}
EDITORS = {"code", "visual studio code", "cursor"}  # terminals only where the focus says so ("Terminal 1, zsh")
NAV = {"up", "down", "left", "right", "tab", "shift+tab", "esc", "pageup", "pagedown", "home", "end"}  # can't delete
KEY_NAMES = {"command": "cmd", "option": "alt", "control": "ctrl", "escape": "esc", "delete": "backspace",
             "enter": "return", "page-up": "pageup", "page-down": "pagedown", "arrow-up": "up", "arrow-down": "down",
             "arrow-left": "left", "arrow-right": "right"}
MODS = ("ctrl", "alt", "shift", "cmd", "fn")

# Local rules, for when Jev can't answer. Shell commands that delete or overwrite something:
DELETES = re.compile(
    r"\b(rm|rmdir|unlink|shred|srm|trash|delete|erase)\b|--?delete\b|\bempty\s+(the\s+)?trash\b"
    r"|\bgit\s+(clean|reset\s+--hard|checkout\s+--|restore|stash\s+drop|branch\s+-D)", re.I)
OVERWRITES = re.compile(
    r"\b(dd|truncate|rsync)\b|\b(sed|perl)\s+(-\S+\s+)*-i|\bln\s+-\S*f|\btee\b(?!\s+-a)"
    r"|\bcurl\b.*\s-o\s|\bwget\b.*\s-O\s|\bgit\s+push\b.*(--force|\s-f\b)", re.I)
# Buttons, menu items and the like that do (macOS writes "Don’t Save" with a curly apostrophe):
RISKY_LABEL = re.compile(
    r"\b(delete|remove|trash|bin|erase|empty|clear|discard|don['’]?t save|overwrite|replace|reset)\b", re.I)

# Secret-looking values, hidden before Jev reads a command or script (they don't change whether it deletes anything).
# Long runs next to a "/" are left alone: they're paths, which matter to the verdict.
VALUE = r"('[^']*'|\"[^\"]*\"|[^\s'\";&|()]+)"  # stops at ; & | ( ): what follows may be a command
SECRETS = [
    (re.compile(r"(?i)(--(?:password|passwd|token|secret|api-?key|auth)(?:=|\s+))" + VALUE), r"\1[hidden]"),
    (re.compile(r"(?i)\b(\w*(?:key|token|secret|password|passwd|pass|auth)\w*=)" + VALUE), r"\1[hidden]"),
    (re.compile(r"(?i)\b(Bearer\s+)[^\s'\"]+"), r"\1[hidden]"),
    (re.compile(r"(\b(?:mysql\w*|mariadb\w*)\b[^\n;&|]*?\s-p)[^\s'\"]+"), r"\1[hidden]"),
    (re.compile(r"(\bsshpass\s+-p\s*)" + VALUE), r"\1[hidden]"),
    (re.compile(r"(?<![\w+/=.-])(?=[\w+=-]*\d)(?=[\w+=-]*[A-Za-z])[\w+=-]{33,}(?![\w+/=-])"), "[hidden]"),
]

WHY = {"deletes": "It deletes something.", "overwrites": "It overwrites something."}
MAYBE = "It may delete or overwrite something."

# One action to check. text: what Jev reads; title, detail: the approval card ("{why}" in the title becomes the reason);
# local: the local rules' reason to ask, or None; cache: keep Jev's verdict for this text for the session;
# fact: a reason to ask that Jev can't know (what's on disk), so it asks without Jev.
Action = namedtuple("Action", "kind text title detail local cache fact", defaults=(False, None))


def hide_secrets(text):
    for pattern, hidden in SECRETS:
        text = pattern.sub(hidden, text)
    return text


# ---- shell commands ----

def full_path(p, cwd=None):
    p = os.path.expandvars(os.path.expanduser(p))
    return p if os.path.isabs(p) else os.path.join(cwd or Path.home(), p)  # the brain runs in your home folder


def start_dir(cmd):
    """The folder a shell command works in: <dir> if it starts with "cd <dir> &&" (or ";"), else None (home)."""
    m = re.match(r"\s*cd\s+((?:'[^']*'|\"[^\"]*\"|[^\s;&|'\"])+)\s*(&&|;)", cmd)
    try:
        return full_path(shlex.split(m[1])[0]) if m else None
    except (ValueError, IndexError):
        return None


def commands(cmd):
    """Each simple command in a shell line, as (words, redirects): quotes removed, "VAR=x" and sudo / env / nohup...
    in front left out, redirects as [(operator, target)]. words can be empty ("> file" alone empties the file).
    Heredoc bodies are left out. Raises ValueError if the quoting doesn't add up."""
    lex = shlex.shlex(without_heredocs(cmd), posix=True, punctuation_chars="".join(OPERATOR))
    lex.whitespace, lex.whitespace_split = " \t\r", True
    tokens = []
    for tok in lex:  # shlex lumps neighbouring punctuation together: ";>" is the end of a command, then a redirect
        mixed = tok and set(tok) <= OPERATOR and set(tok) & {"<", ">"} and set(tok) & (SEPARATOR - {"&"})
        tokens += re.findall(r">\||[;|()\n]+|[^;|()\n]+", tok) if mixed else [tok]  # but ">|" is one
    found, words, redirects = [], [], []
    for i, tok in enumerate(tokens + [";"]):
        if tok and set(tok) <= OPERATOR and set(tok) & {"<", ">"}:  # a redirect (a quoted "a > b" is a word)
            if words and words[-1].isdigit() and i and tokens[i - 1] == words[-1]:
                words.pop()  # "2>": the descriptor, not an argument
            redirects.append((tok, tokens[i + 1] if i + 1 < len(tokens) else ""))
        elif tok and set(tok) <= SEPARATOR:  # the end of one command ("" is a quoted empty argument)
            if words or redirects:
                found.append((_without_prefixes(words), redirects))
            words, redirects = [], []
        elif not (redirects and i and tokens[i - 1] == redirects[-1][0]):  # not a redirect's target
            words.append(tok)
    return found


def without_heredocs(cmd):
    """cmd without its heredoc bodies (the lines after <<EOF, up to the EOF line): text, not shell, so an apostrophe in
    them can't upset the splitting. Only the splitting uses this; Jev reads the whole command."""
    lines, kept, i = cmd.split("\n"), [], 0
    while i < len(lines):
        kept.append(lines[i])
        i += 1
        for dash, _, marker in HEREDOC.findall(kept[-1]):
            end = next((j for j in range(i, len(lines)) if (lines[j].lstrip("\t") if dash else lines[j]) == marker), None)
            i = i if end is None else end + 1  # no closing line: not a heredoc after all, keep what follows
    return "\n".join(kept)


def _without_prefixes(words):
    while words and (re.fullmatch(r"\w+=.*", words[0]) or Path(words[0]).name in PREFIXES):
        takes_value, follow = PREFIXES.get(Path(words[0]).name, (set(), 0))
        words = words[1:]
        while words and words[0].startswith("-"):  # sudo -E, sudo -u alex, env -i, nice -n 10
            words = words[2:] if words[0] in takes_value else words[1:]
        words = words[follow:]  # timeout 30s
    return words


def writes(op, target):
    """Does this redirect write a file? Not >>, 2>&1, >&- or anything in /dev/."""
    return op in WRITES and bool(target) and not target.startswith("/dev/") and not (op == ">&" and target in ("1", "2", "-"))


def replaces_existing(cmd):
    """A fact about the disk: why this shell command replaces something that already exists, or None."""
    cwd = start_dir(cmd)
    try:
        parts = commands(cmd)
    except ValueError:
        return "I can't tell what it changes."
    for words, redirects in parts:
        # "> file" (1>, 2>, &>, >|) replaces the file if it exists; ">>" appends; 2>&1 and /dev/ are fine
        for op, target in redirects:
            if writes(op, target) and os.path.exists(full_path(target, cwd)):
                return f"It replaces the existing file {target}."
        if words and Path(words[0]).name in COPIES:  # cp / mv / ditto / install onto something that already exists
            paths = [w for w in words[1:] if not w.startswith("-")]
            if len(paths) >= 2:
                dest = full_path(paths[-1], cwd)
                targets = ([os.path.join(dest, os.path.basename(p.rstrip("/"))) for p in paths[:-1]]
                           if os.path.isdir(dest) else [dest])
                if any(os.path.exists(t) for t in targets):
                    return f"It replaces something that already exists in {paths[-1]}."
    return None


def may_replace(line):
    """For a command typed into a terminal, whose folder isn't known: MAY_REPLACE if it writes a file with ">" or
    copies or moves onto a path (cp / mv / ditto / install with two or more), else None."""
    try:
        parts = commands(line)
    except ValueError:
        return MAY_REPLACE if re.search(r"(?<![<>&])>(?![>&])|\b(cp|mv|ditto|install)\s", line) else None
    for words, redirects in parts:
        if any(writes(op, target) for op, target in redirects):
            return MAY_REPLACE
        for i, w in enumerate(words):  # anywhere: a terminal's line starts with its prompt
            if Path(w).name in COPIES and len([p for p in words[i + 1:] if not p.startswith("-")]) >= 2:
                return MAY_REPLACE
    return None


def script_files(cmd):
    """Full paths of the local script files a shell command runs: bash x.sh, python3 x.py, ./x, source x."""
    cwd, found = start_dir(cmd), []
    try:
        parts = commands(cmd)
    except ValueError:
        return []
    for words, _ in parts:
        if not words:
            continue
        if INTERPRETER.fullmatch(words[0] if words[0] in (".", "source") else Path(words[0]).name):
            for w in words[1:]:
                if w in ("-c", "-e", "-m"):  # inline code or a module: already in the command
                    break
                if not w.startswith("-"):
                    found.append(os.path.normpath(full_path(w, cwd)))
                    break
        elif "/" in words[0]:
            found.append(os.path.normpath(full_path(words[0], cwd)))
    return found


def script_text(path):
    """The start of a script: up to SCRIPT_CHARS of a text file, or "" (missing, a folder, binary)."""
    if not os.path.isfile(path):
        return ""  # also keeps a named pipe from blocking the read
    try:
        with open(path, "rb") as f:
            head = f.read(SCRIPT_CHARS)
    except OSError:
        return ""
    return "" if b"\0" in head else head.decode("utf-8", "replace").strip()


def with_scripts(text, paths):
    """text plus the text of each script (Jev reads what it does), and the local rules' reason to ask from them."""
    scripts = [(p, t) for p in paths[:3] if (t := script_text(p))]
    local = next((r for _, t in scripts if (r := command_rule(t))), None)
    return text + "".join(f"\nwhich runs the script {p}:\n{t}" for p, t in scripts), local


def command_rule(cmd):
    """Local rule for a shell command: why it needs your OK, or None."""
    return WHY["deletes"] if DELETES.search(cmd) else WHY["overwrites"] if OVERWRITES.search(cmd) else None


# ---- what's on screen, in words ----

def label_rule(*els):
    """Local rule for buttons and the like: a risky word in the label, or in the dialog it sits in ("OK" in
    "Delete 5 items?")."""
    return MAYBE if any(el and RISKY_LABEL.search(f"{el.get('label') or ''} {el.get('dialog') or ''}")
                        for el in els) else None


def normal_key(combo):
    """'Shift+Command+Delete' -> 'shift+cmd+backspace': the mac server's key names, modifiers in Apple's order."""
    parts = [KEY_NAMES.get(p, p) for p in (p.strip().lower() for p in combo.split("+")) if p]
    return "+".join(sorted({p for p in parts if p in MODS}, key=MODS.index) + [p for p in parts if p not in MODS])


def runs_line(combo):
    """Does this key make a terminal run its command line? Any return, ctrl+m, ctrl+j."""
    return combo.split("+")[-1] == "return" or combo in RETURNS


def is_terminal(app, focus=None):
    """A terminal app, or Code / Cursor with the focus in their terminal pane."""
    name, focus = (app or "").strip().lower(), focus or {}
    return name in TERMINALS or (name in EDITORS and "terminal" in f"{focus.get('role')} {focus.get('label')}".lower())


def readable(el):
    return bool(el and el.get("label"))


def element_text(el, where=True):
    """button 'Delete Chat' in window 'WhatsApp' (WhatsApp): an ax.py element in words. A click target without a label:
    what is known, "an unlabelled row in window 'Documents' (Finder)". where=False (a key's focus): no window or app,
    and just the role ("text area"; "an unlabelled text area" read as harmless, cmd+s there fell from 0.78 to 0.06)."""
    if not el:
        return "an unlabelled element"
    role = ax.role_word(el.get("role") or "") or "element"
    text = f"{role} '{el['label']}'" if el.get("label") else f"an unlabelled {role}" if where else role
    if el.get("dialog"):
        text += f" in dialog '{el['dialog']}'"
    if where and el.get("window"):
        text += f" in window '{el['window']}'"
    if where and el.get("app"):
        text += f" ({el['app']})"
    return text


def named(el):
    """"Delete Chat" in WhatsApp: an element for the approval card."""
    if not el:
        return "here"
    name = f"\"{el['label']}\"" if el.get("label") else f"the {ax.role_word(el.get('role') or '') or 'element'}"
    return f"{name} in {el['app']}" if el.get("app") else name


def default_button_text(press, default, dialog):
    """Return presses the default button whatever has the focus, so that's what Jev reads (with the focused Cancel
    named too, Delete scored 0.56 to 0.64)."""
    return f"{press}, which presses the default button '{default}'" + (f" in dialog '{dialog}'" if dialog else "")


# ---- one Action per kind of tool call ----

def click_action(el, button="left"):
    verb = {"right": "right-click", "double": "double-click"}.get(button, "click")
    text = f"{verb} {element_text(el)}"
    # Unlabelled targets all read alike (iPhone Mirroring, a canvas): Jev judges each one, no verdict is kept.
    return Action("click", text, f"{verb.capitalize()} {named(el)}?", text, label_rule(el), cache=readable(el))


def drag_action(src, dst):
    """Both ends: dragging a file onto the Dock's Trash deletes it."""
    text = f"drag {element_text(src)} onto {element_text(dst)}"
    return Action("drag", text, f"Drag {named(src)} onto {named(dst)}?", text, label_rule(src, dst),
                  cache=readable(src) and readable(dst))


def key_action(combo, focus, app, line="", typed=""):
    """combo: normal_key(). focus: ax.focused(), for return and space with "default" (the button return presses) and
    "modal" (in a dialog or sheet). line: in a terminal, the command line this key would run (with its prompt);
    typed: what was typed of it, which says best whether it may replace a file (a prompt can end in ">")."""
    focus = focus or {}
    press = f"press {combo}" + (f" in {app}" if app else "")
    focused = f", focused: {element_text(focus, where=False)}" if focus.get("role") or focus.get("label") else ""
    if line:  # a command: never cached, like Bash
        return Action("key", f"{press}{focused}, which runs the command line: {line}",
                      f"Run this command in {app}? {{why}}", line, command_rule(line), fact=may_replace(typed or line))
    default = (focus.get("default") or "") if combo == "return" else ""  # space presses the focused button instead
    unread = focus.get("modal") and not default and not focus.get("label")  # a dialog, but nothing in it can be read
    text = (default_button_text(press, default, focus.get("dialog")) if default
            else press + focused + (", in a dialog that can't be read" if unread else ""))
    mods, key = combo.split("+")[:-1], combo.split("+")[-1]
    local = (WHY["deletes"] if key == "backspace" and (not mods or "cmd" in mods)  # on a selected file, email, photo
             else label_rule(focus, {"label": default}) if combo in PRESSES else None)
    title = f"Press {combo}" + (f" for \"{default}\"" if default else "") + (f" in {app}?" if app else "?")
    return Action("key", text, title, text, local, cache=not unread)


def one_line(text):
    return "; ".join(line.strip() for line in text.splitlines() if line.strip())


def terminal_action(cmd, app):
    """Text with a newline typed into a terminal runs it as a shell command. Its folder isn't known, so writing a file
    with ">" or copying onto one asks without Jev (may_replace)."""
    line = one_line(cmd)
    return Action("type", f"run shell command in {app}: {line}", f"Run this command in {app}? {{why}}", line,
                  command_rule(line), fact=may_replace(cmd))


def typed_action(text, app):
    """Text with a newline that reads like a shell command, typed into an app that isn't a known terminal (an
    unlisted terminal, or a chat or editor where it's just words, like "if a > b:": Jev tells them apart)."""
    text = one_line(text)
    return Action("type", f"type '{text}' and press return in {app}", f"Type this in {app}? {{why}}", text,
                  command_rule(text))


def command_action(cmd):
    """Bash. What's on disk is a fact Jev can't know, so "> existing file" and cp / mv / ditto / install onto something
    that exists ask without Jev. A script the command runs is read locally and added to what Jev reads."""
    text, script_local = with_scripts(f"run shell command: {cmd}", script_files(cmd))
    return Action("bash", text, "Run this command? {why}", cmd, command_rule(cmd) or script_local,
                  fact=replaces_existing(cmd))


def open_action(target):
    """open_app. An app by name (or from /Applications, /System) goes ahead: None. A URL or a file is judged; a script,
    or an app from anywhere else, runs code: judged with the script's text, never cached."""
    if not re.search(r"[/:]", target):
        return None  # an app name
    path = os.path.normpath(full_path(target)) if target.startswith(("/", "~")) else ""
    app = path.endswith(".app")
    if app and path.startswith(SYSTEM_APPS):
        return None
    runs = app or path.lower().endswith(RUNNABLE)
    text, local = with_scripts(f"open {target}", [path] if runs else [])
    return Action("open", text, f"Open {Path(path).name if path else target}? {{why}}", f"open {target}", local,
                  cache=not runs)


def tool_action(short, args):
    """A mac tool not known here (a new one): described generically and judged, never cached."""
    said = ", ".join(f"{k}={v!r}" for k, v in args.items())
    text = f"use {short} with {said}" if said else f"use {short}"
    return Action("tool", text, f"Use {short}? {{why}}", text, command_rule(said) or label_rule({"label": said}))


# ---- click by name (brain.py's ui_press, ui_menu and ui_set_text) ----

def press_action(label, app, role="button", dialog=""):
    """Pressing a listed control by name. Unlabelled ones all read alike: judged each time, like unlabelled clicks.
    dialog: the message of the alert or sheet it sits in, as for clicks ("OK" in "Delete 5 items?")."""
    where = f" in dialog '{dialog}'" if dialog else ""
    text = f"press {role} '{label}'{where} in {app}" if label else f"press an unlabelled {role}{where} in {app}"
    name = f"\"{label}\"" if label else f"the {role}"
    return Action("press", text, f"Press {name} in {app}?", text, label_rule({"label": label, "dialog": dialog}),
                  cache=bool(label))


def menu_action(path, app):
    """Choosing a menu item by path, like "File > Move to Trash"."""
    path = " > ".join(p.strip() for p in path.split(">") if p.strip())
    text = f"choose menu {path} in {app}"
    return Action("menu", text, f"Choose {path} in {app}?", text,
                  label_rule({"label": path.split(">")[-1]}), cache=True)


def replace_action(label, app, role="text field", one_line=False):
    """Setting the text of a field that already has some. A document or note (a text area, or several lines) reads as
    replacing it; a one-line field (a search, an address) as typing into it, like the type tool. Live, Jev read any
    "replace" as overwriting (a Spotify search 0.96, Safari's address 1.00) and typing as safe (a TextEdit document
    0.04), so the wording carries the difference."""
    field = f"{role} '{label}'" if label else role
    name = f"\"{label}\"" if label else f"the {role}"
    text = (f"type new text into {field} in {app}, which already has text in it" if one_line
            else f"replace the text in {field} in {app}")
    return Action("replace", text, f"Replace the text in {name} in {app}?", text,
                  label_rule({"label": label}), cache=bool(label))


def secret_hid(text):
    """The local rules' reason to ask if hiding secrets took away the very words that make it risky ("AUTH_CMD=rm
    -rf x" reads as "AUTH_CMD=[hidden] -rf x"), else None: then Jev can't be trusted with it."""
    reason = command_rule(text)
    return reason if reason and not command_rule(hide_secrets(text)) else None


def verdict(p, choice):
    """The reason to ask, from Jev's answer, or None to go ahead."""
    return (WHY.get(choice) or MAYBE) if p >= RISK_MIN else None


# ---- reading the screen ----

_ax_thread = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ax")  # its own: the default one also looks up Jev
_reading = None  # the ax.py read in progress (a concurrent.futures.Future)


async def read(fn, *args):
    """An ax.py read on its own thread (AX calls block). None if it takes too long (a hung app), or if the last read
    is still stuck: the target then counts as unreadable. Any other error reaches check(), which asks."""
    global _reading
    if _reading is not None and not _reading.done():
        return None
    _reading = _ax_thread.submit(fn, *args)
    try:
        return await asyncio.wait_for(asyncio.wrap_future(_reading), AX_TIMEOUT_S)
    except TimeoutError:
        return None


class Safety:
    def __init__(self, router, log, use_jev=True):
        """router: a router.Router, or None without a Typesafe key. use_jev: False = local rules only."""
        self.router, self.log, self.use_jev = router, log, use_jev
        self.verdicts = {}  # action text -> Jev's (p_risky, choice), for clicks and keys
        self.typed = ""     # typed since the last click or key: a terminal's command line if it can't be read
        self.typed_in = ""  # the app it was typed into (expect_app): typing elsewhere starts afresh

    async def check(self, name, args):
        """(title, detail) for the approval card if this tool call needs your yes, None to go ahead."""
        t0 = time.monotonic()
        try:
            action = await self.describe(name, args)
            return await self.decide(action, t0) if action else None
        except Exception as e:  # a bug here must never let an action through unchecked
            self.log(f"safety: check failed ({type(e).__name__}), asking")
            return "Go ahead with this?", f"{name.split('__')[-1]} {args}"

    async def describe(self, name, a):
        """The Action to check for this tool call, or None if it can just go ahead (no reading, no Jev)."""
        if name == "Bash":
            return command_action(a.get("command", ""))
        short = name.split("__")[-1]
        if name.startswith("mcp__companion__"):  # ui_controls and remember change nothing on screen
            handler = {"ui_press": self._press, "ui_menu": self._menu, "ui_set_text": self._set_text}.get(short)
            return await handler(a) if handler else None
        if not name.startswith("mcp__mac__") or short in UNCHECKED:
            return None
        handler = {"open_app": self._open, "type": self._type, "click": self._click, "drag": self._drag,
                   "key": self._key}.get(short)
        return await handler(a) if handler else tool_action(short, a)

    async def _open(self, a):
        return open_action(str(a.get("target", "")))

    async def _type(self, a):
        """Typing goes ahead, except a newline in a terminal (a command), or a newline after something shell-like."""
        if a.get("expect_app", "") != self.typed_in:
            self.typed, self.typed_in = "", a.get("expect_app", "")
        self.typed += a.get("text", "")
        if not re.search(r"[\r\n]", a.get("text", "")):
            return None
        typed, self.typed = self.typed, ""
        focus = await read(ax.focused)
        app = (focus or {}).get("app") or a.get("expect_app", "")
        if is_terminal(app, focus):
            return terminal_action(typed, app)
        return typed_action(typed, app) if command_rule(typed) or may_replace(typed) else None

    async def _click(self, a):
        self.typed = ""
        return click_action(await self._element_at(a.get("x"), a.get("y")), a.get("button") or "left")

    async def _drag(self, a):
        self.typed = ""
        src = await self._element_at(a.get("x1"), a.get("y1"))
        return drag_action(src, await self._element_at(a.get("x2"), a.get("y2")))

    async def _key(self, a):
        combo = normal_key(a.get("combo", ""))
        if combo in NAV:
            return None  # what was typed stays: tab may complete it, and return may run it next
        typed = self.typed if a.get("expect_app", "") == self.typed_in else ""  # typed into this app, not another
        self.typed = ""
        focus = await read(ax.focused, combo in PRESSES)
        if combo == "backspace" and (focus or {}).get("role") in ax.TEXT_ROLES:
            return None  # erasing what's being typed in a field, like typing (Jev reads any backspace as deleting)
        app = (focus or {}).get("app") or a.get("expect_app", "")
        line = ((await read(ax.last_line)) or typed.strip()) if is_terminal(app, focus) and runs_line(combo) else ""
        return key_action(combo, focus, app, line, typed.strip())

    async def _press(self, a):
        self.typed = ""
        item = ax.control(a.get("id"))  # what the listing found
        if not item:
            return None  # an old id: nothing gets pressed
        now = await read(ax.control, a.get("id"), False, True)  # the dialog it sits in now, as for clicks
        return press_action(item["label"], item["app"], ax.role_word(item["role"]), (now or {}).get("dialog") or "")

    async def _menu(self, a):
        self.typed = ""
        return menu_action(str(a.get("path", "")), str(a.get("app", "")))

    async def _set_text(self, a):
        """Text into an empty field goes ahead, like typing. If the field has text, or can't be read now, it's judged."""
        item = ax.control(a.get("id"))
        if not item:
            return None  # an id from an older listing: nothing gets typed
        now = await read(ax.control, a.get("id"), True)
        if now and now["role"] in ax.TEXT_ROLES and not now.get("value", "").strip():
            return None
        one_line = bool(now) and now["role"] in ax.TEXT_ROLES - {"AXTextArea"} and "\n" not in now["value"].strip()
        return replace_action(item["label"], item["app"], ax.role_word(item["role"]), one_line)

    async def _element_at(self, x, y):
        """ax.element_at, read once more after a moment if nothing labelled was found."""
        el = await read(ax.element_at, x, y)
        if not readable(el):
            await asyncio.sleep(RETRY_S)
            el = await read(ax.element_at, x, y) or el
        return el

    async def decide(self, action, t0=None):
        """(title, detail) if the action needs your yes, else None. Logs one line: kind, verdict, how, seconds."""
        t0 = t0 or time.monotonic()
        if action.fact:
            why, how = action.fact, "disk"
        elif why := secret_hid(action.text):
            how = "local: a secret hid it"
        elif action.cache and action.text in self.verdicts:
            p, choice = self.verdicts[action.text]
            why, how = verdict(p, choice), f"cached {p:.2f}"
        elif (jev := await self._judge(action.text)) is None:
            why, how = action.local, "local"
        else:
            p, choice = jev
            why, how = verdict(p, choice), f"jev {p:.2f}"
            if action.cache:
                if len(self.verdicts) >= CACHE_MAX:
                    self.verdicts.clear()
                self.verdicts[action.text] = jev
        self.log(f"safety: {action.kind} {'ask' if why else 'allow'} ({how}), {took(t0)}"
                 + (f": {action.text}" if DEBUG else ""))
        return (action.title.replace("{why}", why).strip(), action.detail) if why else None

    async def _judge(self, text):
        """Jev's (p_risky, choice), or None if it can't answer (then the local rules decide)."""
        if not self.use_jev:
            return None  # "jev_safety": false
        if self.router is None or not self.router.enabled:
            self.log("safety: Jev unavailable (no working key), used local rules")
            return None
        try:
            return await self.router.judge(hide_secrets(text))
        except Exception as e:
            self.log(f"safety: Jev unavailable ({type(e).__name__}), used local rules")
            return None
