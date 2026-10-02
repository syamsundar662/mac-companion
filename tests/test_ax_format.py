"""Reading and pressing an app's controls by name (ax.py), against a fake Accessibility tree: nothing real is read,
pressed or typed."""
from types import SimpleNamespace

import pytest

import ax
from ax import format_listing, label_for


def test_label_prefers_title_then_description_then_value():
    assert label_for({"AXTitle": "Send", "AXDescription": "send button"}) == "Send"
    assert label_for({"AXTitle": "", "AXDescription": "Search"}) == "Search"
    assert label_for({"AXPlaceholderValue": "Type a message"}) == "Type a message"


def test_listing_is_compact():
    items = [{"id": 3, "role": "AXButton", "label": "Send"}, {"id": 4, "role": "AXTextField", "label": "Search"}]
    assert format_listing(items) == "3 button 'Send'\n4 text field 'Search'"


@pytest.fixture
def tree(monkeypatch):
    """A fake app: attrs {(element, attribute): value}; t.done records presses and values set."""
    t = SimpleNamespace(attrs={}, done=[], refuse=set())

    def perform(el, action):
        t.done.append((el, action))
        return 1 if el in t.refuse else 0

    def set_value(el, name, value):
        t.done.append((el, name, value))
        return 1 if el in t.refuse else 0
    monkeypatch.setattr(ax, "_get", lambda el, name: t.attrs.get((el, name)))
    monkeypatch.setattr(ax, "AS", SimpleNamespace(AXUIElementPerformAction=perform,
                                                  AXUIElementSetAttributeValue=set_value))
    monkeypatch.setattr(ax, "app_element", lambda name: ("app", "TextEdit") if name.lower() == "textedit" else None)
    ax._cache.clear()
    return t


def build(t, el, role, kids=(), **attrs):
    t.attrs[(el, "AXRole")] = role
    t.attrs[(el, "AXChildren")] = list(kids)
    for k, v in attrs.items():
        t.attrs[(el, f"AX{k}")] = v


def textedit(t):
    build(t, "app", "AXApplication", AXWindows=["win"])
    t.attrs[("app", "AXWindows")] = ["win"]
    build(t, "win", "AXWindow", ["toolbar", "scroll"], Title="Untitled")
    build(t, "toolbar", "AXToolbar", ["bold", "blank", "font"])
    build(t, "bold", "AXButton", Description="Bold")
    build(t, "blank", "AXButton")  # nothing to call it by: left out
    build(t, "font", "AXPopUpButton", Title="Helvetica")
    build(t, "scroll", "AXScrollArea", ["text"])
    build(t, "text", "AXTextArea", Value="Dear Alex, the secret plan is")  # what's typed is not its name


def test_controls_are_listed_with_ids(tree):
    textedit(tree)
    items = ax.list_controls("TextEdit")
    assert [(i["role"], i["label"]) for i in items] == [("AXButton", "Bold"), ("AXPopUpButton", "Helvetica"),
                                                         ("AXTextArea", "")]
    assert format_listing(items).splitlines()[-1].endswith("text area ''")  # a field can be typed into unnamed
    assert ax.control(items[0]["id"]) == {"role": "AXButton", "label": "Bold", "app": "TextEdit"}
    assert ax.control(items[2]["id"], value=True)["value"] == "Dear Alex, the secret plan is"  # read now


def test_no_such_app(tree):
    assert ax.list_controls("Nonexistent") is None


def test_ids_are_never_reused(tree):
    textedit(tree)
    first = ax.list_controls("TextEdit")
    second = ax.list_controls("TextEdit")
    assert not {i["id"] for i in first} & {i["id"] for i in second}
    assert ax.control(first[0]["id"]) is None and not ax.press(first[0]["id"])  # an old listing's id does nothing
    assert tree.done == []


def test_the_listing_stops_at_the_limit(tree):
    textedit(tree)
    assert len(ax.list_controls("TextEdit", limit=2)) == 2


def test_press_and_set_text(tree):
    textedit(tree)
    bold, _, text = ax.list_controls("TextEdit")
    assert ax.press(bold["id"]) and tree.done == [("bold", "AXPress")]
    assert ax.set_text(text["id"], "hello")
    assert tree.done[1:] == [("text", "AXFocused", True), ("text", "AXValue", "hello")]
    tree.refuse.add("bold")
    assert not ax.press(bold["id"])
    assert not ax.press(999) and not ax.set_text(999, "x")


def menus(t):
    build(t, "app", "AXApplication")
    t.attrs[("app", "AXMenuBar")] = "bar"
    build(t, "bar", "AXMenuBar", ["apple", "file", "format"])
    build(t, "apple", "AXMenuBarItem", Title="Apple")
    build(t, "file", "AXMenuBarItem", ["file menu"], Title="File")
    build(t, "file menu", "AXMenu", ["new", "save as", "revert"])
    build(t, "new", "AXMenuItem", Title="New", Enabled=True)
    build(t, "save as", "AXMenuItem", Title="Save As…", Enabled=True)
    build(t, "revert", "AXMenuItem", Title="Revert To", Enabled=False)
    build(t, "format", "AXMenuBarItem", ["format menu"], Title="Format")
    build(t, "format menu", "AXMenu", ["font"])
    build(t, "font", "AXMenuItem", ["font menu"], Title="Font", Enabled=True)
    build(t, "font menu", "AXMenu", ["show fonts"])
    build(t, "show fonts", "AXMenuItem", Title="Show Fonts", Enabled=True)


def test_a_menu_item_is_chosen_by_its_path(tree):
    menus(tree)
    assert ax.menu("TextEdit", "Format > Font > Show Fonts") is None
    assert tree.done == [("show fonts", "AXPress")]  # only the item itself: no menus open on the way


def test_menu_names_match_loosely(tree):
    menus(tree)
    assert ax.menu("textedit", "file > save as...") is None and tree.done == [("save as", "AXPress")]


def test_why_a_menu_item_could_not_be_chosen(tree):
    menus(tree)
    assert ax.menu("TextEdit", "File > Print") == "File has no 'Print'. It has: New, Save As…, Revert To."
    assert ax.menu("TextEdit", "File > Revert To") == "'Revert To' is greyed out."
    assert ax.menu("Nonexistent", "File > New") == "No app called 'Nonexistent' is running."
    assert ax.menu("TextEdit", "File") == "File is a menu, not a menu item."
    tree.refuse.add("new")
    assert ax.menu("TextEdit", "File > New") == "TextEdit didn't take the press."
    assert tree.done == [("new", "AXPress")]


def test_app_element_prefers_an_exact_name_and_never_mac_itself(monkeypatch):
    class App:
        def __init__(self, name, pid):
            self.name, self.pid = name, pid

        def localizedName(self):
            return self.name

        def processIdentifier(self):
            return self.pid
    apps = [App("Mac", ax.os.getpid()), App("Music Helper", 2), App("Music", 3)]
    monkeypatch.setattr(ax, "NSWorkspace", SimpleNamespace(
        sharedWorkspace=lambda: SimpleNamespace(runningApplications=lambda: apps)))
    monkeypatch.setattr(ax, "AS", SimpleNamespace(AXUIElementCreateApplication=lambda pid: f"app {pid}"))
    monkeypatch.setattr(ax, "_wake", lambda pid: None)
    assert ax.app_element("music") == ("app 3", "Music")
    assert ax.app_element("Mus") == ("app 2", "Music Helper")
    assert ax.app_element("mac") is None and ax.app_element(" ") is None
