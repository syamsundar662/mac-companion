import re

import pytest

from speech_chunks import SentenceStreamer


def test_emits_complete_sentences_only():
    s = SentenceStreamer(min_chars=12)
    assert s.feed("Sure thing, opening it. Now") == ["Sure thing, opening it."]
    assert s.feed(" playing") == []
    assert s.flush() == ["Now playing"]


def test_short_sentences_wait_for_more():
    s = SentenceStreamer(min_chars=12)
    assert s.feed("Done. ") == []            # too short to be worth a separate say
    assert s.feed("Spotify is up. ") == ["Done. Spotify is up."]


def test_decimals_and_tamil():
    s = SentenceStreamer(min_chars=5)
    assert s.feed("It is 3.5 degrees. ") == ["It is 3.5 degrees."]
    assert s.feed("நல்லா இருக்கேன்! நீ ") == ["நல்லா இருக்கேன்!"]


def test_danda_ends_a_sentence():
    s = SentenceStreamer(min_chars=5)
    assert s.feed("नमस्ते, आप कैसे हैं। मैं") == ["नमस्ते, आप कैसे हैं।"]
    assert s.flush() == ["मैं"]


CODE_REPLY = "Run this:\n```\nls -la /tmp\necho hi\n```\nThat lists files."


def stream(pieces, min_chars=12):
    s = SentenceStreamer(min_chars=min_chars)
    chunks = []
    for p in pieces:
        chunks += s.feed(p)
    return chunks + s.flush()


@pytest.mark.parametrize("pieces", [
    re.findall(r"\s*\S+", CODE_REPLY),                            # word by word, like Claude's deltas
    [CODE_REPLY[i:i + 1] for i in range(len(CODE_REPLY))],
    [CODE_REPLY[i:i + 3] for i in range(0, len(CODE_REPLY), 3)],
    [CODE_REPLY],
])
def test_never_cuts_inside_a_code_block(pieces):
    chunks = stream(pieces)
    assert all(c.count("```") % 2 == 0 for c in chunks), chunks
    assert chunks == ["Run this:\n```\nls -la /tmp\necho hi\n```", "That lists files."]


def test_closing_quotes_and_brackets_stay_with_their_sentence():
    assert SentenceStreamer(min_chars=5).feed('He said "Open it." Then') == ['He said "Open it."']
    assert SentenceStreamer(min_chars=5).feed("(see below.) Then") == ["(see below.)"]
    assert SentenceStreamer(min_chars=5).feed("She said “Done.” Next") == ["She said “Done.”"]


def test_decimal_split_across_deltas():
    s = SentenceStreamer(min_chars=12)
    assert s.feed("It is 3.") == []
    assert s.feed("5 degrees. ") == ["It is 3.5 degrees."]


def test_sentence_at_end_of_delta_waits_for_next_delta_or_flush():
    s = SentenceStreamer(min_chars=12)
    assert s.feed("Opening Safari now.") == []
    assert s.feed(" Done") == ["Opening Safari now."]
    assert s.flush() == ["Done"]

    s = SentenceStreamer(min_chars=12)
    assert s.feed("Opening Safari now.") == []
    assert s.flush() == ["Opening Safari now."]


def test_lone_newline_yields_nothing():
    s = SentenceStreamer(min_chars=12)
    assert s.feed("\n") == []
    assert s.flush() == []
