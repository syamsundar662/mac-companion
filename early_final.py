"""The final transcription, started at the first short pause instead of after the whole pause (0.6 s) is confirmed."""


class EarlyFinal:
    """A final transcription started at the first short pause, reused if you didn't speak again."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.voiced, self.text, self.done, self.waiter = None, None, False, None

    def start(self, voiced):
        """voiced: the Listener's count of speech blocks when the audio was taken."""
        self.reset()
        self.voiced = voiced

    @property
    def running(self):
        return self.voiced is not None and not self.done

    def result(self, text):
        self.text, self.done = text, True
        if self.waiter:
            waiter, self.waiter = self.waiter, None
            waiter(text)

    def usable(self, final_voiced):
        return self.voiced is not None and final_voiced == self.voiced

    def when_ready(self, fn):
        if self.done:
            fn(self.text)
        else:
            self.waiter = fn
