import queue
import threading
import time

import numpy as np

from voice import BLOCK, SR


class LiveMic:
    """Plays a clip into a Listener like a microphone would, with trailing silence so the Listener can end."""
    is_open = True

    def __init__(self, audio, realtime=True, tail_s=1.0, backlog_s=0.0):
        """audio: 16 kHz mono float32 in [-1, 1]. realtime: feed at real speed (False: as fast as possible).
        backlog_s: this much is already waiting when you subscribe, like voice.Mic replaying what it recorded
        during the wake check."""
        raw = np.asarray(audio)
        # int16 PCM would pass as float32 but sound 32768 times too loud, so refuse it rather than guess.
        if raw.ndim != 1 or np.issubdtype(raw.dtype, np.integer):
            raise ValueError(f"LiveMic wants 16 kHz mono float32 in [-1, 1], got {raw.dtype} {raw.shape}")
        # A real mic keeps sending after you stop talking; without a tail the Listener waits for an end that never comes.
        self.audio = np.concatenate([np.asarray(raw, dtype=np.float32), np.zeros(round(tail_s * SR), np.float32)])
        self.realtime, self.backlog_s = realtime, backlog_s
        self.count = 0
        self.stalled = False
        self.t0 = None  # time.monotonic() the first block fed stands for (backlog: before you subscribed)

    def subscribe(self, since=None):
        """Queue of (number, block), numbered from 1 like voice.Mic. With `since`, starts after that block."""
        q = queue.Queue()
        self.stalled = False
        items = [(n, self.audio[i:i + BLOCK].copy())
                 for n, i in enumerate(range(0, len(self.audio) - BLOCK + 1, BLOCK), 1) if since is None or n > since]
        backlog = round(self.backlog_s * SR / BLOCK)
        for n, block in items[:backlog]:  # queued before returning, as voice.Mic does
            self.count = n
            q.put((n, block))
        self.t0 = time.monotonic() - backlog * BLOCK / SR

        def feed():
            for j, (n, block) in enumerate(items[backlog:], backlog):
                if self.realtime:
                    # Sleep to a deadline: sleeping BLOCK / SR each time adds up the overhead and runs slow.
                    time.sleep(max(0.0, self.t0 + j * BLOCK / SR - time.monotonic()))
                self.count = n
                q.put((n, block))
            self.stalled = True  # out of audio: a Listener still waiting gives up like it would on a dead mic
        threading.Thread(target=feed, daemon=True).start()
        return q

    def unsubscribe(self, q):
        pass

    def restart(self):
        pass
