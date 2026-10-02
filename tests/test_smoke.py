from voice import dedupe, wake_rest, words_key
from companion import copy_examples, is_stop, script_language


def test_tamil_survives_filters():
    t = "வணக்கம், இன்று வானிலை எப்படி இருக்கிறது?"
    assert dedupe(t) == t and words_key(t)


def test_wake_and_stop():
    assert wake_rest("Hey Mac, open Spotify.", ["mac", "mack"]) == "open Spotify"
    assert wake_rest("Hey man, what's up?", ["mac", "mack"]) is None
    assert is_stop("Can you just stop listening?") and not is_stop("stop the music")
    assert script_language("நன்றி") == "ta" and script_language("നന്ദി") == "ml"


def test_copy_examples_fills_in_only_missing_files(tmp_path):
    (tmp_path / "config.example.json").write_text("{}")
    (tmp_path / "persona.example.md").write_text("generic")
    (tmp_path / "persona.md").write_text("mine")
    copy_examples(tmp_path)
    assert (tmp_path / "config.json").read_text() == "{}" and (tmp_path / "persona.md").read_text() == "mine"
    assert not (tmp_path / "memory.md").exists()  # no example, nothing made up
