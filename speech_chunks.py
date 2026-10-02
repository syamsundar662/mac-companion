"""Streamed reply text -> speakable sentences, so Mac can start talking before Claude has finished writing."""
import re

# End mark (plus any closing quotes or brackets) followed by whitespace, or a newline. "3.5" doesn't count.
BOUNDARY = re.compile(r"[.!?।][\"')\]”’]*(?=\s)|\n")


class SentenceStreamer:
    def __init__(self, min_chars=12):
        self.min_chars = min_chars
        self.buf = ""

    def feed(self, delta):
        """Add a text delta; return zero or one chunks: the complete sentences so far,
        cut at the last boundary at or after min_chars and never inside a ``` code block."""
        self.buf += delta
        cut = None
        for m in BOUNDARY.finditer(self.buf):
            if m.end() >= self.min_chars and self.buf.count("```", 0, m.end()) % 2 == 0:
                cut = m.end()
        if cut is None:
            return []
        chunk, self.buf = self.buf[:cut].strip(), self.buf[cut:].lstrip()
        return [chunk] if chunk else []

    def flush(self):
        """End of the reply: return zero or one chunks, whatever is left."""
        chunk, self.buf = self.buf.strip(), ""
        return [chunk] if chunk else []
