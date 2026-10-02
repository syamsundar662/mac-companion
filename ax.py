"""Read what's on screen through macOS Accessibility (what VoiceOver uses): what a click would hit and what has the
keyboard focus, for the safety check (safety.py); and click by name: read an app's buttons, menus and fields and act on
them directly, much faster and steadier than a screenshot, a look and a pixel click. Needs the Accessibility permission
(Terminal's, today). AX calls block: run them in a thread."""
import collections
import itertools
import os
import re
import time

import ApplicationServices as AS
import objc
import Quartz
from AppKit import NSRunningApplication, NSWorkspace

ACTIONABLE = {"AXButton", "AXMenuItem", "AXMenuBarItem", "AXCheckBox", "AXRadioButton", "AXPopUpButton",
              "AXMenuButton", "AXLink", "AXTextField", "AXTextArea", "AXSearchField", "AXComboBox", "AXTab",
              "AXCell", "AXRow", "AXDisclosureTriangle", "AXIncrementor", "AXSlider"}
ROLE_WORDS = {"AXButton": "button", "AXMenuItem": "menu item", "AXTextField": "text field", "AXTextArea": "text area",
              "AXSearchField": "search field", "AXCheckBox": "checkbox", "AXLink": "link", "AXPopUpButton": "pop-up",
              "AXTab": "tab", "AXRow": "row", "AXCell": "cell", "AXMenuButton": "menu button",
              "AXMenuBarItem": "menu bar item", "AXDockItem": "Dock item", "AXStaticText": "text",
              "AXRadioButton": "radio button", "AXWebArea": "web area", "AXScrollArea": "scroll area"}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXSearchField", "AXComboBox"}  # their AXValue is what's typed, not a name
CLIMB = 4  # levels from what's hit up to the button, row or menu item it belongs to
TEXT_MAX = 80  # window titles, like labels (label_for): a web page writes its own title, so it gets little room
TAIL_CHARS = 4000  # of a terminal's text: enough for the command line being typed
MESSAGE_TIMEOUT_S = 0.3  # one AX call to a hung app gives up after this
DIALOGS = {"AXDialog", "AXSystemDialog"}
_woken = set()  # pids asked to expose their whole tree
_cache = {}  # id -> {"el", "role", "label", "app"}: the controls of the most recent listing (list_controls)
_ids = itertools.count(1)  # never reused, so an id from an older listing can't press something else

AS.AXUIElementSetMessagingTimeout(AS.AXUIElementCreateSystemWide(), MESSAGE_TIMEOUT_S)  # for every app (default 6 s)


def _get(el, name):
    err, value = AS.AXUIElementCopyAttributeValue(el, name, None)
    return value if err == 0 else None


def label_for(attrs):
    for key in ("AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue", "AXHelp"):
        v = attrs.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:80]
    return ""


def role_word(role):
    return ROLE_WORDS.get(role, role[2:].lower() if role.startswith("AX") else role)


def _label(el, role):
    keys = ("AXTitle", "AXDescription", "AXPlaceholderValue", "AXHelp") + (() if role in TEXT_ROLES else ("AXValue",))
    return label_for({k: _get(el, k) for k in keys})


def _pid(el):
    err, pid = AS.AXUIElementGetPid(el, None)
    return pid if err == 0 else None


def _wake(pid):
    """Chrome and Electron apps leave their web content unlabelled until asked (once per app). Not
    AXEnhancedUserInterface: Chrome takes either, and that one also slows window animations in other apps."""
    if pid and pid not in _woken:
        _woken.add(pid)
        AS.AXUIElementSetAttributeValue(AS.AXUIElementCreateApplication(pid), "AXManualAccessibility", True)


def _app_name(pid):
    app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
    return (app.localizedName() or "") if app else ""


def _hit(root, x, y):
    err, el = AS.AXUIElementCopyElementAtPosition(root, float(x), float(y), None)
    return el if err == 0 else None


def _window_under(x, y):
    """(pid, app, window title) of the topmost window at (x, y), Mac itself (bubble, glow, panel) left out, or None."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:  # front to back
        b, pid = w.get("kCGWindowBounds") or {}, w.get("kCGWindowOwnerPID")
        if pid == os.getpid() or not w.get("kCGWindowAlpha") or w.get("kCGWindowOwnerName") == "Window Server":
            continue
        if b.get("X", 0) <= x < b.get("X", 0) + b.get("Width", 0) and b.get("Y", 0) <= y < b.get("Y", 0) + b.get("Height", 0):
            return pid, w.get("kCGWindowOwnerName") or "", (w.get("kCGWindowName") or "").strip()[:TEXT_MAX]
    return None


def _actionable(el):
    for _ in range(CLIMB + 1):
        if el is None:
            return None
        if (_get(el, "AXRole") or "") in ACTIONABLE:
            return el
        el = _get(el, "AXParent")
    return None


def _modal(top):
    """A sheet, dialog or alert."""
    return top is not None and (_get(top, "AXRole") in ("AXSheet", "AXDialog") or _get(top, "AXSubrole") in DIALOGS)


def _message(top):
    """The text of an alert or sheet ("Do you want to replace it?"), or ""."""
    texts = [_get(k, "AXValue") for k in (_get(top, "AXChildren") or []) if _get(k, "AXRole") == "AXStaticText"]
    return " ".join(t.strip() for t in texts if isinstance(t, str) and t.strip())[:150]


def _dialog_text(el):
    """The message of the alert or sheet el sits in, or ""."""
    top = _get(el, "AXTopLevelUIElement")  # its window, sheet or drawer
    return _message(top) if _modal(top) else ""


def _info(el, hit=None):
    role = _get(el, "AXRole") or ""
    label = _label(el, role)
    if not label and hit is not None and hit is not el:  # a row or cell named by the text in it
        label = _label(hit, _get(hit, "AXRole") or "")
    win = _get(el, "AXWindow")
    title = (_get(win, "AXTitle") or "") if win is not None else ""
    return {"role": role, "label": label, "window": title.strip()[:TEXT_MAX] if isinstance(title, str) else "",
            "dialog": _dialog_text(el), "app": _app_name(_pid(el))}


def element_at(x, y):
    """What a click at screen point (x, y) would hit: {"role", "label", "window", "dialog", "app"}. Mac's own windows
    (the bubble, the glow) are looked through to the app below. If AX can't read the spot, what the window list knows
    (app, window title); None if no other app has a window there."""
    with objc.autorelease_pool():  # runs on safety.py's own thread: free what each read made
        under = _window_under(x, y)
        if under:
            _wake(under[0])  # before the first hit test: Chrome and Electron label web content only once asked
        hit = _hit(AS.AXUIElementCreateSystemWide(), x, y)
        if hit is None or _pid(hit) == os.getpid():
            hit = _hit(AS.AXUIElementCreateApplication(under[0]), x, y) if under else None
        if hit is None:
            return {"role": "", "label": "", "window": under[2], "dialog": "", "app": under[1]} if under else None
        _wake(_pid(hit))
        return _info(_actionable(hit) or hit, hit)


def _focus():
    """(app, element) with the keyboard focus, Mac itself left out. Either can be None."""
    app = _get(AS.AXUIElementCreateSystemWide(), "AXFocusedApplication")
    if app is None or _pid(app) == os.getpid():
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        pid = front.processIdentifier() if front else None
        app = AS.AXUIElementCreateApplication(pid) if pid and pid != os.getpid() else None
    if app is not None:
        _wake(_pid(app))
    return app, (_get(app, "AXFocusedUIElement") if app is not None else None)


def _default(app, el):
    """(label of the button return would press, is the focus in a dialog or sheet?, that dialog's message): the default
    button of an open sheet, of what el sits in, or of the focused window."""
    win = _get(app, "AXFocusedWindow")
    sheets = [k for k in (_get(win, "AXChildren") or []) if _get(k, "AXRole") == "AXSheet"] if win is not None else []
    tops = sheets + [t for t in (_get(el, "AXTopLevelUIElement") if el is not None else None, win) if t is not None]
    modal = any(_modal(t) for t in tops)
    for top in tops:
        if (button := _get(top, "AXDefaultButton")) is not None:
            return _label(button, "AXButton"), modal, (_message(top) if _modal(top) else "")
    return "", modal, ""


def focused(default=False):
    """What has the keyboard focus, like element_at; only "app" is filled in if the element can't be read.
    default: also "default" (the button return would press, or "") and "modal" (in a dialog or sheet), and "dialog"
    from the default button's dialog if the focus can't say.
    None if no app can be read."""
    with objc.autorelease_pool():
        app, el = _focus()
        if app is None:
            return None
        info = _info(el) if el is not None else {"role": "", "label": "", "window": "", "dialog": "",
                                                 "app": _app_name(_pid(app))}
        if default:
            info["default"], info["modal"], message = _default(app, el)
            info["dialog"] = info["dialog"] or message
        return info


def _tail(el):
    """The last TAIL_CHARS of el's text: just that range if the app can give it (a terminal's whole scrollback can be
    megabytes), else the whole value."""
    n = _get(el, "AXNumberOfCharacters")
    if isinstance(n, int) and n > 0:
        where = AS.AXValueCreate(AS.kAXValueCFRangeType, AS.CFRangeMake(max(0, n - TAIL_CHARS), min(n, TAIL_CHARS)))
        err, text = AS.AXUIElementCopyParameterizedAttributeValue(el, "AXStringForRange", where, None)
        if err == 0 and isinstance(text, str):
            return text
    text = _get(el, "AXValue")
    return text[-TAIL_CHARS:] if isinstance(text, str) else ""


def last_line():
    """The last non-empty line of the focused text: in a terminal, the command being typed (with its prompt)."""
    with objc.autorelease_pool():
        _, el = _focus()
        text = _tail(el) if el is not None else ""
        return next((line.strip() for line in reversed(text.splitlines()) if line.strip()), "")[:200]


# ---- click by name ----

def format_listing(items):
    return "\n".join(f"{i['id']} {role_word(i['role'])} '{i['label']}'" for i in items)


def app_element(name):
    """(AX element, name) of the running app called name, else the first whose name starts with it; Mac itself left
    out. None if there's none."""
    name = name.strip().lower()
    apps = [a for a in NSWorkspace.sharedWorkspace().runningApplications() if a.processIdentifier() != os.getpid()]
    app = next((a for a in apps if (a.localizedName() or "").lower() == name), None) or next(
        (a for a in apps if (a.localizedName() or "").lower().startswith(name)), None)
    if not name or app is None:
        return None
    _wake(app.processIdentifier())  # Chrome and Electron apps expose their web content only once asked
    return AS.AXUIElementCreateApplication(app.processIdentifier()), app.localizedName()


def list_controls(app_name, limit=250, depth=30, budget_s=3.0):
    """The buttons, fields, menus, rows... in the app's front two windows, as [{"id", "role", "label"}] for press() and
    set_text(); a field is listed even without a name (its text is not its name). None if no such app is running."""
    with objc.autorelease_pool():
        found = app_element(app_name)
        if found is None:
            return None
        root, app = found
        _cache.clear()
        items, deadline = [], time.monotonic() + budget_s  # a huge web page can't hold it up
        todo = collections.deque((w, 0) for w in (_get(root, "AXWindows") or [])[:2])
        while todo and len(items) < limit and time.monotonic() < deadline:
            el, d = todo.popleft()
            role = _get(el, "AXRole") or ""
            if role in ACTIONABLE and ((label := _label(el, role)) or role in TEXT_ROLES):
                _cache[i := next(_ids)] = {"el": el, "role": role, "label": label, "app": app}
                items.append({"id": i, "role": role, "label": label})
            if d < depth:
                todo.extend((k, d + 1) for k in (_get(el, "AXChildren") or []))
        return items


def control(item_id, value=False, dialog=False):
    """A listed control: {"role", "label", "app"}; value=True also reads a field's text now ("value"); dialog=True the
    message of the alert or sheet it sits in now ("dialog", or ""). None if the id isn't from the most recent listing."""
    item = _cache.get(item_id)
    if item is None:
        return None
    info = {k: item[k] for k in ("role", "label", "app")}
    if value and item["role"] in TEXT_ROLES:
        with objc.autorelease_pool():
            value = _get(item["el"], "AXValue")
        info["value"] = value if isinstance(value, str) else ""
    if dialog:
        with objc.autorelease_pool():
            info["dialog"] = _dialog_text(item["el"])
    return info


def press(item_id):
    item = _cache.get(item_id)
    return item is not None and AS.AXUIElementPerformAction(item["el"], "AXPress") == 0


def set_text(item_id, text):
    """Replace the text in a listed field."""
    item = _cache.get(item_id)
    if item is None:
        return False
    AS.AXUIElementSetAttributeValue(item["el"], "AXFocused", True)
    return AS.AXUIElementSetAttributeValue(item["el"], "AXValue", text) == 0


def _menu_name(title):
    """'Save As…' and 'save as...' alike."""
    return re.sub(r"(\.\.\.|…)$", "", " ".join(str(title or "").split())).strip().lower()


def _menu_items(el):
    kids = _get(el, "AXChildren") or []
    if len(kids) == 1 and _get(kids[0], "AXRole") == "AXMenu":  # step into its menu
        kids = _get(kids[0], "AXChildren") or []
    return kids


def menu(app_name, path):
    """Choose a menu item by its path, like "File > New Window" or "Format > Font > Show Fonts". Only the item itself is
    pressed (menus can be read closed). None when done, else why not, in words."""
    with objc.autorelease_pool():
        found = app_element(app_name)
        if found is None:
            return f"No app called '{app_name}' is running."
        el, parent = _get(found[0], "AXMenuBar"), "The menu bar"
        for part in [p for p in path.split(">") if p.strip()]:
            kids = _menu_items(el) if el is not None else []
            hit = next((k for k in kids if _menu_name(_get(k, "AXTitle")) == _menu_name(part)), None)
            if hit is None:
                names = [t for t in (_get(k, "AXTitle") for k in kids) if isinstance(t, str) and t.strip()]
                return f"{parent} has no '{part.strip()}'. It has: {', '.join(names)}."
            el, parent = hit, _get(hit, "AXTitle")
        if el is None or _get(el, "AXRole") != "AXMenuItem" or any(
                _get(k, "AXRole") == "AXMenu" for k in _get(el, "AXChildren") or []):
            return f"{parent} is a menu, not a menu item."
        if _get(el, "AXEnabled") is False:
            return f"'{parent}' is greyed out."
        return None if AS.AXUIElementPerformAction(el, "AXPress") == 0 else f"{found[1]} didn't take the press."
