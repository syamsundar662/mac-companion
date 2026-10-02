"""Claude's click-by-name tools (brain.py) and their safety check (safety.py), with a fake Jev and fake ax.py: nothing
is read, pressed or typed for real."""
import asyncio

import pytest

import ax
import brain
import safety
from tests.test_safety import FakeJev, can_use, check, make_brain
from safety import Safety

CONTROLS = {1: {"role": "AXButton", "label": "Delete Chat", "app": "WhatsApp"},
            2: {"role": "AXButton", "label": "Send", "app": "WhatsApp"},
            3: {"role": "AXTextField", "label": "Search", "app": "Spotify"},
            4: {"role": "AXTextArea", "label": "", "app": "TextEdit"}}


@pytest.fixture
def controls(monkeypatch):
    """ax.control answers from CONTROLS; values: what each field holds now. reads counts the value reads."""
    values, reads = {3: "", 4: "Dear Alex"}, []

    def control(item_id, value=False, dialog=False):
        item = CONTROLS.get(item_id)
        if item is None:
            return None
        if value:
            reads.append(item_id)
            return {**item, "value": values.get(item_id, "")}
        return {**item, "dialog": DIALOGS.get(item_id, "")} if dialog else dict(item)
    monkeypatch.setattr(ax, "control", control)
    return values, reads


DIALOGS = {5: "Are you sure you want to delete 5 items? You can't undo this action."}
CONTROLS[5] = {"role": "AXButton", "label": "OK", "app": "Finder"}


def test_a_press_in_a_dialog_names_the_dialog_like_a_click(controls):
    jev = FakeJev(by_text={"press button 'OK' in dialog '" + DIALOGS[5] + "' in Finder": 0.97})
    assert check(Safety(jev, lambda line: None), "mcp__companion__ui_press", {"id": 5}) == (
        'Press "OK" in Finder?', "press button 'OK' in dialog '" + DIALOGS[5] + "' in Finder")
    jev = FakeJev(error=RuntimeError("down"))  # no Jev: the local rules see "delete" in the dialog
    assert check(Safety(jev, lambda line: None), "mcp__companion__ui_press", {"id": 5})


def test_a_risky_press_asks_a_safe_one_goes_ahead(controls):
    jev = FakeJev(by_text={"press button 'Delete Chat' in WhatsApp": 0.98})
    safe = Safety(jev, lambda line: None)
    assert check(safe, "mcp__companion__ui_press", {"id": 1}) == (
        'Press "Delete Chat" in WhatsApp?', "press button 'Delete Chat' in WhatsApp")
    assert check(safe, "mcp__companion__ui_press", {"id": 2}) is None
    check(safe, "mcp__companion__ui_press", {"id": 1})  # the same button again: Jev isn't asked twice
    assert jev.asked == ["press button 'Delete Chat' in WhatsApp", "press button 'Send' in WhatsApp"]


def test_an_unlabelled_control_is_judged_every_time(controls):
    jev = FakeJev()
    safe = Safety(jev, lambda line: None)
    for _ in range(2):
        assert check(safe, "mcp__companion__ui_press", {"id": 4}) is None
    assert jev.asked == ["press an unlabelled text area in TextEdit"] * 2


def test_a_menu_is_described_by_its_path(controls):
    jev = FakeJev(p=0.95)
    safe = Safety(jev, lambda line: None)
    title, detail = check(safe, "mcp__companion__ui_menu", {"app": "Finder", "path": "File >Move to Trash"})
    assert title == "Choose File > Move to Trash in Finder?"
    assert jev.asked == ["choose menu File > Move to Trash in Finder"]


def test_text_into_an_empty_field_is_like_typing(controls):
    values, reads = controls
    jev = FakeJev(p=1.0)
    assert check(Safety(jev, lambda line: None), "mcp__companion__ui_set_text", {"id": 3, "text": "Coldplay"}) is None
    assert jev.asked == [] and reads == [3]  # the field was read now, not from the listing


def test_replacing_text_in_a_field_is_judged(controls):
    values, _ = controls
    values[3] = "Adele"
    jev = FakeJev(p=0.9, choice="overwrites")
    title, _ = check(Safety(jev, lambda line: None), "mcp__companion__ui_set_text", {"id": 3, "text": "Coldplay"})
    assert title == 'Replace the text in "Search" in Spotify?'
    # A one-line field reads as typing into it (live, every "replace" read as overwriting, a search too)
    assert jev.asked == ["type new text into text field 'Search' in Spotify, which already has text in it"]
    jev = FakeJev()
    check(Safety(jev, lambda line: None), "mcp__companion__ui_set_text", {"id": 4, "text": "Hi"})
    assert jev.asked == ["replace the text in text area in TextEdit"]  # a document


def test_several_lines_in_a_text_field_read_as_replacing(controls):
    values, _ = controls
    values[3] = "line one\nline two"
    jev = FakeJev()
    check(Safety(jev, lambda line: None), "mcp__companion__ui_set_text", {"id": 3, "text": "x"})
    assert jev.asked == ["replace the text in text field 'Search' in Spotify"]


def test_an_unreadable_field_counts_as_full(controls, monkeypatch):
    async def stuck(fn, *args):
        return None  # a hung app: safety.read gave up
    monkeypatch.setattr(safety, "read", stuck)
    jev = FakeJev()
    check(Safety(jev, lambda line: None), "mcp__companion__ui_set_text", {"id": 3, "text": "x"})
    assert jev.asked == ["replace the text in text field 'Search' in Spotify"]


def test_an_old_id_does_nothing_so_nothing_is_checked(controls):
    jev = FakeJev(p=1.0)
    safe = Safety(jev, lambda line: None)
    assert check(safe, "mcp__companion__ui_press", {"id": 99}) is None
    assert check(safe, "mcp__companion__ui_set_text", {"id": 99, "text": "x"}) is None
    assert jev.asked == []


@pytest.mark.parametrize("name, args, asks", [
    ("mcp__companion__ui_press", {"id": 1}, True), ("mcp__companion__ui_press", {"id": 2}, False),
    ("mcp__companion__ui_menu", {"app": "Finder", "path": "File > Move to Trash"}, True),
    ("mcp__companion__ui_menu", {"app": "TextEdit", "path": "File > New"}, False)])
def test_local_rules_when_jev_is_down(controls, name, args, asks):
    safe = Safety(FakeJev(error=TimeoutError()), lambda line: None)
    assert bool(check(safe, name, args)) == asks


def test_reading_controls_is_never_checked(controls):
    jev = FakeJev(p=1.0)
    assert check(Safety(jev, lambda line: None), "mcp__companion__ui_controls", {"app": "WhatsApp"}) is None
    assert jev.asked == []


# ---- brain ----

def test_a_risky_press_asks_then_clears_the_screen(monkeypatch, controls):
    b = make_brain(monkeypatch, FakeJev(p=0.98))
    assert isinstance(can_use(b, "mcp__companion__ui_press", {"id": 1}), brain.PermissionResultAllow)
    assert b.events == [("ask", 'Press "Delete Chat" in WhatsApp?'), "clear screen"]
    b.events.clear()
    b.answer_with = False
    assert isinstance(can_use(b, "mcp__companion__ui_menu", {"app": "Finder", "path": "File > Move to Trash"}),
                      brain.PermissionResultDeny)


def test_listing_controls_and_remembering_need_no_check(monkeypatch, controls):
    jev = FakeJev(p=1.0)
    b = make_brain(monkeypatch, jev)
    for name in ("mcp__companion__ui_controls", "mcp__companion__remember"):
        assert isinstance(can_use(b, name, {"app": "WhatsApp", "fact": "x"}), brain.PermissionResultAllow)
    assert jev.asked == [] and b.events == []


def test_the_ui_tools_use_the_screen():
    for short in ("ui_controls", "ui_press", "ui_set_text", "ui_menu"):
        assert brain.touches_screen(f"mcp__companion__{short}", {})
    assert not brain.touches_screen("mcp__companion__remember", {})


def run(tool, args):
    return asyncio.run(tool.handler(args))["content"][0]["text"]


def test_the_tools_call_ax(monkeypatch):
    calls = []
    monkeypatch.setattr(ax, "list_controls", lambda app: calls.append(app) or (
        [{"id": 7, "role": "AXButton", "label": "Send"}] if app == "WhatsApp" else [] if app == "Figma" else None))
    monkeypatch.setattr(ax, "press", lambda i: calls.append(i) or i == 7)
    monkeypatch.setattr(ax, "set_text", lambda i, text: calls.append((i, text)) or True)
    monkeypatch.setattr(ax, "menu", lambda app, path: calls.append((app, path)) or None)
    assert run(brain.ui_controls, {"app": "WhatsApp"}) == "7 button 'Send'"
    assert "No app called 'Nope'" in run(brain.ui_controls, {"app": "Nope"})
    assert "screenshot" in run(brain.ui_controls, {"app": "Figma"})
    assert run(brain.ui_press, {"id": 7}) == "Pressed."
    assert "ui_controls" in run(brain.ui_press, {"id": 8})
    assert run(brain.ui_set_text, {"id": 7, "text": "hi"}) == "Done."
    assert run(brain.ui_menu, {"app": "TextEdit", "path": "File > New"}) == "Done."
    assert calls == ["WhatsApp", "Nope", "Figma", 7, 8, (7, "hi"), ("TextEdit", "File > New")]


def test_the_prompt_offers_them_only_when_on(monkeypatch):
    b = make_brain(monkeypatch, FakeJev())
    assert "First try `ui_controls`" in b._system_prompt()
    b.cfg["ax_tools"] = False
    assert "ui_controls" not in b._system_prompt()


def test_the_bubble_says_what_is_pressed(monkeypatch):
    monkeypatch.setitem(ax._cache, 5, {"el": None, "role": "AXButton", "label": "Send", "app": "WhatsApp"})
    assert brain.describe("mcp__companion__ui_press", {"id": 5}) == "Pressing Send"
    assert brain.describe("mcp__companion__ui_menu", {"path": "File > New"}) == "Choosing File > New"
    assert brain.describe("mcp__companion__ui_controls", {"app": "Spotify"}) == "Reading Spotify"


def test_ax_control_reads_the_dialog_now(monkeypatch):
    monkeypatch.setattr(ax, "_cache", {9: {"el": "ok-button", "role": "AXButton", "label": "OK", "app": "Finder"}})
    monkeypatch.setattr(ax, "_dialog_text", lambda el: "Delete 5 items?" if el == "ok-button" else "")
    assert ax.control(9, dialog=True) == {"role": "AXButton", "label": "OK", "app": "Finder", "dialog": "Delete 5 items?"}
    assert "dialog" not in ax.control(9)
