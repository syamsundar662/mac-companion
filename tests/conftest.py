from types import SimpleNamespace

import pytest

import ax
import router


@pytest.fixture(autouse=True)
def nothing_real_from_router(monkeypatch):
    """No test may open an app, change the volume or dark mode, or press a media key, even after a refactor:
    everything router.do() reaches the system through fails the test. Tests that need do() record it themselves."""
    def refuse(*args, **kwargs):
        pytest.fail("a test reached the real system through router.py")
    monkeypatch.setattr(router, "osascript", refuse)
    monkeypatch.setattr(router, "press_media_key", refuse)
    monkeypatch.setattr(router, "subprocess", SimpleNamespace(run=refuse))


class Refuse:
    def __init__(self, what):
        self.what = what

    def __getattr__(self, name):
        pytest.fail(f"a test read the real screen through ax.{self.what}.{name}: use the screen fixture or fakes")


@pytest.fixture(autouse=True)
def no_real_screen_reads(request, monkeypatch):
    """No test may read the real screen through ax.py (Accessibility, the window list, the running apps): it must use
    the screen fixture (tests/test_safety.py) or fake those objects. Only the live checks (*_live.py) may."""
    if request.module.__name__.endswith("_live"):
        return
    for name in ("AS", "Quartz", "NSWorkspace", "NSRunningApplication"):
        monkeypatch.setattr(ax, name, Refuse(name))
