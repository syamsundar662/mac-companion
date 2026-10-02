"""Voice lock: learns your voice from a few sentences and checks whether later speech is you.

What's stored is a voiceprint (256 numbers in voiceprint.npy), never a recording. Everything runs offline with
sherpa-onnx and a small speaker model (models/wespeaker_en_voxceleb_resnet34_LM.onnx, about 26 MB).
"""
import os
import tempfile
import threading
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
MODEL = HERE / "models/wespeaker_en_voxceleb_resnet34_LM.onnx"
PROFILE = HERE / "voiceprint.npy"
SR = 16000

SENTENCES = [
    "The quick brown fox jumps over the lazy dog near the river.",
    "I usually start my morning with a hot cup of tea and some music.",
    "Please open my email and show me the messages from today.",
    "Remind me to call my friend when I get back home this evening.",
]


class VoiceID:
    def __init__(self, threshold=0.55, path=PROFILE):
        self.threshold = threshold
        self.path = Path(path)
        self.profile = np.load(self.path) if self.path.exists() else None
        self._extractor = None
        self._lock = threading.Lock()

    @property
    def enrolled(self):
        return self.profile is not None

    def _embed(self, audio):
        import sherpa_onnx
        with self._lock:
            if self._extractor is None:
                self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                    sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(MODEL), num_threads=2))
            stream = self._extractor.create_stream()
            stream.accept_waveform(SR, audio)
            stream.input_finished()
            v = np.array(self._extractor.compute(stream), dtype=np.float32)
        return v / (np.linalg.norm(v) or 1.0)

    def warm_up(self):
        """Load the model now, so the first check after startup isn't slowed down by it."""
        if self.enrolled:
            self._embed(np.zeros(SR, dtype=np.float32))

    def check(self, audio):
        """(is it you?, similarity). Always "yes" when no voiceprint is set up, since there's nothing to compare."""
        if self.profile is None:
            return True, None
        sim = float(self._embed(audio) @ self.profile)
        limit = self.threshold - (0.05 if len(audio) < SR else 0.0)  # "Hey Mac" alone is short and matches a bit less
        return sim >= limit, sim

    def enroll(self, clips):
        """Build and save the voiceprint. Returns how well each clip matches it (all should be high)."""
        embeddings = [self._embed(c) for c in clips]
        p = np.mean(embeddings, axis=0)
        p /= np.linalg.norm(p)
        self._save(p)
        return [float(e @ p) for e in embeddings]

    def adapt(self, audio, min_score=0.6, rate=0.05):
        """Learn from a request that is certainly you (hold ⌥). Returns (score, updated)."""
        return self.learn(self._embed(audio), min_score, rate) if self.profile is not None else (None, False)

    def learn(self, embedding, min_score=0.6, rate=0.05):
        """Nudge the voiceprint towards this sample, unless it scores too low to trust (noise, someone else)."""
        sim = float(embedding @ self.profile)
        if sim < min_score:
            return sim, False
        p = (1 - rate) * self.profile + rate * embedding
        self._save((p / np.linalg.norm(p)).astype(np.float32))
        return sim, True

    def _save(self, p):
        """Write via a temp file and a rename, so a crash mid-write never leaves a broken voiceprint."""
        target = self.path.resolve()  # write through a symlink, don't replace it
        fd, tmp = tempfile.mkstemp(dir=target.parent, suffix=".npy")
        try:
            with os.fdopen(fd, "wb") as f:
                np.save(f, p)
            os.replace(tmp, target)
        except BaseException:
            os.unlink(tmp)
            raise
        self.profile = p

    def forget(self):
        self.path.unlink(missing_ok=True)
        self.profile = None
