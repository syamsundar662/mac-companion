"""Ears and mouth: the microphone, the wake word ("Hey Mac"), local Whisper speech recognition, and spoken replies."""
import collections
import contextlib
import queue
import re
import subprocess
import threading
import time
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import sounddevice as sd

SR = 16000
BLOCK = 480               # 30 ms of audio
BLOCK_S = BLOCK / SR
PREROLL_S = 1.5           # audio kept from just before listening starts, so a quick start isn't clipped

# Listening to a request
CALIBRATE_S = 0.3         # measure the room for the level meter
END_SILENCE_S = 0.6       # stop this long after you stop talking
NO_SPEECH_S = 6.0         # give up if you never start
MAX_S = 45.0
MIN_SPEECH_BLOCKS = 5     # about 0.15 s the speech detector is sure is a voice
MIN_LEVEL = 0.006         # floor for the level meter
DEAF_S = 0.4              # ignore the wake chime

# Spotting the wake word
WAKE_END_SILENCE_S = 0.5
WAKE_MAX_S = 5.0
WAKE_MIN_VOICED = 4

VAD_MODEL = Path(__file__).resolve().parent / "models/silero_vad.onnx"

# Whisper likes to "hear" these in near-silence.
JUNK = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye", "so"}
GREETINGS = {"hey", "hi", "hello", "ok", "okay", "yo", "a", "hay", "hei", "hee", "ey", "oi"}
FILLER = re.compile(r"^(wake up|week up|are you (there|awake|up)|you there|listen|please|come on)\b[ ,.!?]*", re.I)


def pick_mic(preferred=""):
    """Prefer the Mac's built-in mic: holding an AirPods mic open would drop AirPods audio to phone-call quality."""
    inputs = [d for d in sd.query_devices() if d["max_input_channels"] > 0]
    for want in ([preferred] if preferred else []) + ["MacBook", "Built-in"]:
        for d in inputs:
            if want.lower() in d["name"].lower():
                return d["index"]
    return None  # system default


def rms(block):
    return float(np.sqrt(np.mean(block ** 2)))


class SpeechDetector:
    """Silero VAD: is this bit of audio a human voice? Unlike a loudness check it ignores fans, hum, taps and music
    beds, so a noisy room can't keep Mac listening. About 0.1 ms per 32 ms of audio. One per thread (it has state)."""
    WINDOW = 512

    def __init__(self):
        import sherpa_onnx
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = str(VAD_MODEL)
        cfg.silero_vad.threshold = 0.5
        cfg.silero_vad.min_silence_duration = 0.1
        cfg.silero_vad.min_speech_duration = 0.1
        cfg.silero_vad.window_size = self.WINDOW
        cfg.sample_rate = SR
        cfg.num_threads = 1
        self.model = sherpa_onnx.VadModel.create(cfg)
        self.buf = np.zeros(0, dtype=np.float32)

    def reset(self):
        self.model.reset()
        self.buf = np.zeros(0, dtype=np.float32)

    def feed(self, block):
        """True if the audio just added contains speech."""
        self.buf = np.concatenate([self.buf, block])
        speech = False
        while len(self.buf) >= self.WINDOW:
            speech |= bool(self.model.is_speech(self.buf[:self.WINDOW].tolist()))
            self.buf = self.buf[self.WINDOW:]
        return speech


class Mic:
    """One microphone stream shared by the wake-word spotter and the request listener.
    Open only while something is listening."""

    def __init__(self, preferred=""):
        self.preferred = preferred
        self._lock = threading.Lock()
        self._subs = ()           # replaced, never mutated, so the audio callback needs no lock
        self._stream = None
        self.count = 0            # blocks are numbered, so a listener can pick up exactly where another stopped
        self.last_block = time.monotonic()
        self.recent = collections.deque(maxlen=int(PREROLL_S / BLOCK_S))

    def _open(self):
        self._stream = sd.InputStream(device=pick_mic(self.preferred), samplerate=SR, channels=1, dtype="float32",
                                      blocksize=BLOCK, callback=self._callback)
        self._stream.start()
        self.last_block = time.monotonic()

    @property
    def is_open(self):
        return self._stream is not None

    @property
    def stalled(self):
        return self._stream is not None and time.monotonic() - self.last_block > 2.0

    def restart(self):
        """Reopen the stream. After sleep, a device change or a Mic Mode switch it can go silent."""
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.stop()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            if self._subs:
                sd._terminate()   # refresh the device list; indices change when AirPods and the like come and go
                sd._initialize()
                self._open()

    def subscribe(self, since=None):
        """Queue of (number, block). With `since`, first replays recent blocks numbered after it."""
        q = queue.Queue()
        with self._lock:
            if since is not None:
                for item in list(self.recent):
                    if item[0] > since:
                        q.put(item)
            self._subs = self._subs + (q,)
            if self._stream is None:
                self._open()
        return q

    def unsubscribe(self, q):
        with self._lock:
            self._subs = tuple(s for s in self._subs if s is not q)
            if not self._subs and self._stream is not None:
                self._stream.stop()
                self._stream.close()
                self._stream = None
                self.recent.clear()

    def _callback(self, indata, frames, time_info, status):
        self.last_block = time.monotonic()
        self.count += 1
        item = (self.count, indata[:, 0].copy())
        self.recent.append(item)
        for q in self._subs:
            q.put(item)


class Listener:
    """Records one request. Ends on trailing silence, on stop() (keep audio) or cancel() (drop it)."""

    def __init__(self, mic, on_level, on_done):
        self.mic, self.on_level, self.on_done = mic, on_level, on_done
        self._stop = threading.Event()
        self._cancelled = False
        self._speech = None
        self.frames = []       # the audio so far, for live previews (see snapshot)
        self.voiced = 0
        self.quiet_for = 0.0   # seconds since the last speech
        # time.monotonic() when you stopped talking and when listening ended, last request (None if nothing heard)
        self.speech_end = self.listen_end = None
        self.active = False

    def start(self, seed=None, since=None, deaf=False, hold=False):
        """seed: audio already heard (the wake phrase and maybe the start of the request).
        since: mic block number to continue from. deaf: don't count the first moments (the wake chime) as speech.
        hold: you're holding a key; pauses don't end it, only stop() or cancel() do."""
        if self.active:
            return
        self.active, self._cancelled = True, False
        self._stop.clear()
        threading.Thread(target=self._run, args=(seed, since, deaf, hold), daemon=True).start()

    def snapshot(self):
        """Everything heard so far in this request (for showing your words while you're still talking)."""
        frames = list(self.frames)
        return np.concatenate(frames) if frames else None

    def stop(self):
        self._stop.set()

    def cancel(self):
        self._cancelled = True
        self._stop.set()

    def _run(self, seed, since, deaf, hold):
        if self._speech is None:
            self._speech = SpeechDetector()  # made on this thread and only used here
        self._speech.reset()
        frames, noise, voiced, last_voice = [], [], 0, None
        self.frames, self.voiced, self.quiet_for = frames, 0, 0.0
        self.speech_end = self.listen_end = None
        if seed is not None:
            frames = [seed[i:i + BLOCK] for i in range(0, len(seed) - BLOCK + 1, BLOCK)]
            self.frames = frames  # snapshot() includes the seed
            noise = sorted(rms(b) for b in frames)[:10]
            voiced = MIN_SPEECH_BLOCKS
            last_voice = len(frames) * BLOCK_S - WAKE_END_SILENCE_S  # the seed already ends in a pause
        live_start = None
        try:
            q = self.mic.subscribe(since=since)
        except Exception as e:
            self.active = False
            self.on_done(None, f"Microphone error: {e}")
            return
        error = None
        try:
            while not self._stop.is_set():
                try:
                    _, block = q.get(timeout=0.5)
                except queue.Empty:
                    if self.mic.stalled:
                        error = "The microphone stopped sending sound. I've reset it, please try again."
                        break
                    continue
                frames.append(block)
                t = len(frames) * BLOCK_S  # audio time, so a slow mic start can't shorten anything
                if live_start is None and q.empty():
                    live_start = t     # pre-roll drained; from here it's live audio
                level = rms(block)
                if len(noise) < CALIBRATE_S / BLOCK_S:
                    noise.append(level)
                if len(frames) % 3 == 0:  # the level meter only; "is that speech?" is the detector's job
                    scale = min(0.04, max(MIN_LEVEL, 3.0 * float(np.median(noise))))
                    self.on_level(min(1.0, level / (scale * 4)))
                in_chime = deaf and live_start is not None and t - live_start < DEAF_S
                # The speech detector decides; a clearly loud voice (well above the room) also counts, as a safety
                # net so you're never cut off mid-sentence if the detector misses a stretch (e.g. over loud music).
                loud_voice = len(noise) >= 5 and level > max(0.05, 6.0 * float(np.median(noise)))
                if (self._speech.feed(block) or loud_voice) and not in_chime:
                    voiced += 1
                    last_voice = t
                quiet_for = t - (last_voice if last_voice is not None else 0)
                self.voiced, self.quiet_for = voiced, quiet_for
                limit = END_SILENCE_S if voiced >= MIN_SPEECH_BLOCKS else NO_SPEECH_S
                if (quiet_for > limit and not hold) or t > MAX_S:
                    break
            # You stopped talking quiet_for (the pause we waited out) before the last block we read, and blocks
            # still queued (replayed from the wake check) are newer than it. Taken before closing the mic.
            listen_end = time.monotonic()
            speech_end = listen_end - q.qsize() * BLOCK_S - self.quiet_for
        finally:
            self.mic.unsubscribe(q)
        self.active = False
        if error:
            try:
                self.mic.restart()
            except Exception:
                pass
            self.on_done(None, error)
        elif self._cancelled or voiced < MIN_SPEECH_BLOCKS:  # nothing that sounds like a voice (e.g. a silent ⌥ hold)
            self.on_done(None, None)
        else:
            self.speech_end, self.listen_end = speech_end, listen_end
            self.on_done(np.concatenate(frames), None)


class WakeSpotter:
    """Always listening for short bursts of speech; hands each one to on_segment(audio, last_block_number)
    to check for the wake phrase. Silence and steady background noise never reach Whisper."""

    def __init__(self, mic, on_segment, paused, on_error, on_note=lambda msg: None):
        self.mic, self.on_segment, self.paused, self.on_error, self.on_note = mic, on_segment, paused, on_error, on_note
        self._stop = None

    @property
    def running(self):
        return self._stop is not None

    def start(self):
        if self._stop is None:
            self._stop = threading.Event()
            threading.Thread(target=self._run, args=(self._stop,), daemon=True).start()

    def stop(self):
        if self._stop is not None:
            self._stop.set()
            self._stop = None

    def _run(self, stop):
        try:
            q = self.mic.subscribe()
        except Exception as e:
            self._stop = None
            self.on_error(str(e))
            return
        speech = SpeechDetector()
        seg, voiced, quiet = [], 0, 0
        before = collections.deque(maxlen=10)   # 0.3 s before speech starts
        try:
            while not stop.is_set():
                try:
                    n, block = q.get(timeout=0.5)
                except queue.Empty:
                    if self.mic.stalled:
                        self.on_note("microphone went silent, reopening it")
                        try:
                            self.mic.restart()
                        except Exception as e:
                            self.on_note(f"couldn't reopen the microphone: {e}")
                            time.sleep(5)
                    continue
                if self.paused():
                    seg = []
                    before.clear()
                    speech.reset()
                    continue
                is_speech = speech.feed(block)
                if not seg:
                    before.append(block)
                    if is_speech:  # only a voice starts a burst; fans and clatter never reach Whisper
                        seg, voiced, quiet = list(before), 1, 0
                    continue
                seg.append(block)
                if is_speech:
                    voiced, quiet = voiced + 1, 0
                else:
                    quiet += 1
                if quiet * BLOCK_S >= WAKE_END_SILENCE_S or len(seg) * BLOCK_S >= WAKE_MAX_S:
                    if voiced >= WAKE_MIN_VOICED:
                        self.on_segment(np.concatenate(seg), n)
                    seg = []
                    before.clear()
        finally:
            self.mic.unsubscribe(q)


def _words(text):
    return [(m.group().lower(), m.end()) for m in re.finditer(r"[A-Za-z']+", text)]


def _sounds_like(word, names):
    """names[0] is the real name: loose match, but about the same length (so "machine" isn't "maci").
    The rest are spellings Whisper uses for it: near-exact match, or short words like "mate" and "miss" would count."""
    if len(word) < min(4, len(names[0])):  # "may", "mac" and friends are everyday words, not the name
        return False
    for k, n in enumerate(names):
        r = SequenceMatcher(None, word, n).ratio()
        loose = 0.66 if len(n) >= 4 else 0.8  # a 3-letter name like "Mac" must be near-exact, or "man" and "max" count
        if (k == 0 and r >= loose and abs(len(word) - len(n)) <= 2) or (k > 0 and r >= 0.8):
            return True
    return False


def _name_at(words, i, names):
    """How many words at words[i] are the name: 2 for a split name like "may say", 1, or 0 if none."""
    if i + 1 < len(words):
        joined = words[i][0] + words[i + 1][0]
        if any(SequenceMatcher(None, joined, n).ratio() >= 0.9 for n in names):  # joins must be near-exact
            return 2
    return 1 if _sounds_like(words[i][0], names) else 0


def mentions(text, names):
    """Does a name appear anywhere (fuzzy)? Worth a second look with the accurate model."""
    words = _words(text)
    return any(_name_at(words, i, names) for i in range(len(words)))


def wake_rest(text, names):
    """If text starts with the wake phrase ("hey maci", "hi maci", "maci wake up"), return what came after it,
    keeping its capitals (maybe ""). Otherwise None. `names` is the name plus the spellings Whisper uses for it."""
    words = _words(text)
    for i in range(min(4, len(words))):
        n = _name_at(words, i, names)
        if not n:
            continue
        rest = text[words[i + n - 1][1]:].lstrip(" ,.!?;:")
        greeted = i > 0 and words[i - 1][0] in GREETINGS
        if greeted or (i == 0 and (not rest.strip(" .!?") or rest.lower().startswith("wake up"))):
            return FILLER.sub("", rest).strip(" ,.!?")
    return None


class EncodeOnce:
    """Whisper's encoder, about half the work, run once per clip: picking the language and transcribing share it."""

    def __init__(self, encoder):
        self.encoder, self.last = encoder, None

    def __call__(self, mel):
        import mlx.core as mx
        if self.last is None or self.last[0].shape != mel.shape or not mx.array_equal(self.last[0], mel):
            self.last = (mel, self.encoder(mel))
        return self.last[1]


class Stale(Exception):
    """A transcription nobody wants any more (StopWhen)."""


class StopWhen:
    """Whisper's decoder, stopped between tokens once `stale()` says the result is no longer wanted. early_final: you
    spoke again after the early job started, and the final transcription was waiting behind it (0.9 s became 1.9 s)."""

    def __init__(self, decoder):
        self.decoder, self.stale = decoder, None

    def __call__(self, *args, **kwargs):
        if self.stale and self.stale():
            raise Stale
        return self.decoder(*args, **kwargs)


def stop_when(model, stale):
    """Let the next jobs on this (loaded) model be stopped by `stale`; None: not stoppable."""
    if model is not None and hasattr(model, "decoder"):
        if not isinstance(model.decoder, StopWhen):
            model.decoder = StopWhen(model.decoder)
        model.decoder.stale = stale


class Transcriber:
    """Runs Whisper on one dedicated thread (MLX GPU streams are per-thread).
    Two models: a big accurate one for requests, a small fast one for spotting the wake word."""

    def __init__(self, repo, fast_repo, language, prompt="", languages=None, preview_repo=None, one_pass=True):
        """language: a fixed code like "en", or "auto" to pick the most likely of `languages` (e.g. en, ta, ml).
        Letting Whisper choose from all ~100 languages often mistakes accented English for something else.
        one_pass: one decode, no retries at higher temperatures (an unclear clip took 6 decodes, 4 s instead of 1 s),
        and one encoder run shared by language picking and transcribing."""
        self.repo, self.fast_repo, self.prompt, self.one_pass = repo, fast_repo, prompt, one_pass
        # Live previews use a small multilingual model: a few hundred ms, so the final words never wait long behind one.
        self.preview_repo = preview_repo or repo
        self.language = None if language in (None, "", "auto") else language
        self.languages = languages or ["en"]
        self.last_language = self.language or "en"  # what the last request was spoken in
        self.busy_since = None  # when the current job started (lets the app notice if it ever gets stuck)
        self.q = queue.Queue()
        self._preview = None  # (audio, callback): only the newest is kept, and it runs only when nothing else waits
        threading.Thread(target=self._work, daemon=True).start()

    def preview(self, audio, callback):
        """Transcribe what's been said so far, for display only. Skipped whenever real work is waiting."""
        self._preview = (audio, callback)
        self.q.put(None)  # wake the worker

    def _run_preview(self, mlx_whisper, holder, loaded):
        audio, cb = self._preview
        self._preview = None
        self.busy_since = time.monotonic()
        text = ""
        repo = self.preview_repo
        try:
            if repo in loaded:
                holder.model, holder.model_path = loaded[repo], repo
            lang = self.language or self._pick_language(audio, holder, repo)
            r = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language=lang, temperature=0.0,
                                       condition_on_previous_text=False, verbose=None)
            loaded[repo] = holder.model
            text = dedupe((r.get("text") or "").strip())
            if words_key(text) in JUNK:
                text = ""
        except Exception:
            text = ""
        self.busy_since = None
        cb(text)

    def warm_up(self, fast=False):
        self.q.put((np.zeros(SR // 2, dtype=np.float32), None, fast, True, "", None))

    def transcribe(self, audio, callback, fast=False, hint=True, context="", stale=None):
        """hint=False: skip the vocabulary prompt (it can make Whisper "hear" those words in a cough).
        context: what Mac just said, so answers to it ("jsmith42") are spelled the way Mac said them.
        stale: True once the result is no longer wanted. Then the job is skipped if it hasn't started, or stopped
        between tokens if it has, and the callback gets ("", None)."""
        self.q.put((audio, callback, fast, hint, context, stale))

    def _pick_language(self, audio, holder, repo=None):
        """The most likely of your languages, from Whisper's own language scores."""
        if len(self.languages) == 1:
            return self.languages[0]
        import mlx.core as mx
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        model = holder.get_model(repo or self.repo, mx.float16)
        if self.one_pass and not isinstance(model.encoder, EncodeOnce):
            model.encoder = EncodeOnce(model.encoder)
        mel = log_mel_spectrogram(audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        if self.one_pass:  # the first segment exactly as transcribe() cuts it, so EncodeOnce can reuse the encoding
            mel = mel[:min(N_FRAMES, mel.shape[-2] - N_FRAMES)]
        _, probs = model.detect_language(pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16))
        return max(self.languages, key=lambda code: probs.get(code, 0.0))

    def _work(self):
        import mlx_whisper
        from mlx_whisper.transcribe import ModelHolder
        loaded = {}  # mlx_whisper caches one model; keep both and swap them in
        while True:
            job = self.q.get()
            if job is None:  # a preview request: only if no real work is queued behind it
                if self._preview is not None and self.q.empty():
                    self._run_preview(mlx_whisper, ModelHolder, loaded)
                continue
            audio, cb, fast, hint, context, stale = job
            if stale and stale():  # skipped: the job queued behind it (the final transcription) goes first
                if cb:
                    cb("", None)
                continue
            self.busy_since = time.monotonic()
            prompt = f"{context[-300:]} {self.prompt}".strip() if context else self.prompt
            repo = self.fast_repo if fast else self.repo
            text, err = "", None
            try:
                if repo in loaded:
                    ModelHolder.model, ModelHolder.model_path = loaded[repo], repo
                    stop_when(loaded[repo], stale)
                if fast:
                    r = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language="en", temperature=0.0,
                                               condition_on_previous_text=False, verbose=None)
                else:
                    lang = self.language or self._pick_language(audio, ModelHolder)
                    if lang != "en":  # the English word hints would pull Tamil or Malayalam towards English
                        hint, prompt = False, context[-300:]
                    elif not hint:
                        prompt = ""
                    r = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language=lang,
                                               initial_prompt=prompt or None,
                                               **({"temperature": 0.0} if self.one_pass else {}),
                                               condition_on_previous_text=False,
                                               verbose=None)
                    self.last_language = lang
                loaded[repo] = ModelHolder.model
                text = (r.get("text") or "").strip()
            except Stale:
                text = ""
            except Exception as e:
                err = f"Speech recognition failed: {e}"
            finally:
                if stale:
                    stop_when(loaded.get(repo), None)
            self.busy_since = None
            text = dedupe(text)
            if (words_key(text) in JUNK or (hint and echoes_prompt(text, self.prompt))
                    or echoes_context(text, context)):
                text = ""
            if cb:
                cb(text, err)


def echoes_prompt(text, prompt):
    """On unclear audio Whisper can "hear" its own vocabulary hint ("Mac, Gmail, WhatsApp...")."""
    words = re.findall(r"[a-z']+", text.lower())
    hints = set(re.findall(r"[a-z']+", prompt.lower()))
    return len(words) >= 3 and sum(w in hints for w in words) / len(words) >= 0.6  # "WhatsApp" alone is a fine answer


def echoes_context(text, context):
    """Given the conversation as a hint, unclear audio can come back as Mac's own words ("Which account should I...")."""
    norm = lambda s: " ".join(re.findall(r"[a-z0-9']+", s.lower()))
    t = norm(text)
    return len(t.split()) >= 4 and t in norm(context)


def words_key(text):
    """Lowercase words without punctuation, in any alphabet (Tamil, Malayalam...), for comparing sentences.
    Only a-z used to be kept, which made Tamil and Malayalam look empty, and they were thrown away."""
    return " ".join(re.findall(r"\w+", text.lower()))


def dedupe(text):
    """Whisper sometimes loops ("I like the other one. I like the other one. ..."). Keep each sentence once."""
    seen, out = set(), []
    for sentence in re.split(r"(?<=[.!?।])\s+", text):
        key = words_key(sentence)
        if key and key not in seen:
            seen.add(key)
            out.append(sentence)
    return " ".join(out)


SAY_MAX = 600  # characters per `say`; longer text is cut off


def speakable(text):
    text = re.sub(r"https?://\S+", "a link", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"```.*", " ", text, flags=re.S)  # a code block that never closes runs to the end
    text = re.sub(r"[*_`#>\[\]|]", "", text)
    text = re.sub("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:SAY_MAX]


Item = collections.namedtuple("Item", "text voice then t_heard")  # one queued piece of speech


class Speaker:
    """Speaks queued text in order with macOS `say`, one process at a time, on its own thread."""

    def __init__(self, voice="", rate=0, on_note=lambda msg: None):
        self.voice, self.rate, self.on_note = voice, rate, on_note
        self.proc = None
        self.started_at = None  # time.monotonic() when the last `say` got its text
        self.pending = collections.deque()  # Items, text already speakable
        # One lock covers taking an item and starting its `say`, so stop() either empties the queue first or finds
        # the new process to cut off. `gen` counts stops, so a `then` that was about to run is dropped too.
        self.cond = threading.Condition()
        self.gen = 0
        threading.Thread(target=self._run, daemon=True).start()

    @property
    def speaking(self):
        return self.proc is not None and self.proc.poll() is None

    def say(self, text, then=None, voice=None, t_heard=None):
        """Cut off whatever is being said or queued and speak text instead. Nothing speakable: nothing happens."""
        if speakable(text):
            with self.cond:  # one step, so nothing else gets queued in between (the lock is re-entrant)
                self.stop()
                self.enqueue(text, then, voice, t_heard)

    def enqueue(self, text, then=None, voice=None, t_heard=None):
        """Speak text after everything already queued. `then` runs if it finishes without being stopped, on the
        speech thread with the whole queue waiting for it: keep it quick (hand off with on_main).
        With nothing speakable it is a marker: `then` runs when the queue gets to it.
        voice: overrides the usual voice (e.g. Vani for Tamil).
        t_heard: when you stopped talking (time.monotonic()), to log how long the reply took to start."""
        text = speakable(text)
        if text or then:
            with self.cond:
                self.pending.append(Item(text, voice or self.voice, then, t_heard))
                self.cond.notify()

    def drop_pending(self):
        """Forget queued items that haven't started; what is being said goes on.
        Returns the t_heard one of them carried (else None), so a later item can log the first speech."""
        with self.cond:
            t_heard = next((item.t_heard for item in self.pending if item.t_heard is not None), None)
            self.pending.clear()
        return t_heard

    def stop(self):
        """Stop talking and forget everything queued."""
        with self.cond:
            self.pending.clear()
            self.gen += 1
            if self.speaking:
                self.proc.terminate()

    def _run(self):
        while True:
            try:
                self._speak_next()
            except Exception as e:  # a failing `say` must not end the speech thread
                self._note(f"speech failed: {e!r}")

    def _note(self, msg):
        try:
            self.on_note(msg)
        except Exception:
            pass  # a failing log must not end the speech thread either

    def _speak_next(self):
        with self.cond:
            while not self.pending:
                self.cond.wait()
            text, voice, then, t_heard = self.pending.popleft()
            # Join what is already waiting in the same voice: fewer `say` starts, fewer gaps.
            # Never past a `then` (it marks the end of something) and never beyond SAY_MAX (nothing gets cut off).
            while self.pending and not then and self.pending[0].voice == voice:
                joined = f"{text} {self.pending[0].text}".strip()
                if len(joined) > SAY_MAX:
                    break
                more = self.pending.popleft()
                text, then = joined, more.then
                t_heard = more.t_heard if t_heard is None else t_heard
            gen, proc = self.gen, None
            if text:
                cmd = ["say"] + (["-v", voice] if voice else []) + (["-r", str(self.rate)] if self.rate else [])
                proc = self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                                    stderr=subprocess.DEVNULL)
                # `say` can quit before reading (e.g. a voice that isn't installed): its exit code is logged below.
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.write(text.encode())
                with contextlib.suppress(BrokenPipeError):
                    proc.stdin.close()  # closes the pipe even when sending the text fails
                # When `say` got its text: a lower bound on audible speech (process start and synthesis add a little).
                self.started_at = time.monotonic()
        if proc and t_heard is not None:
            self._note(f"timing: first speech {self.started_at - t_heard:.1f}s after you stopped")
        rc = proc.wait() if proc else 0
        if rc not in (0, -15):  # -15: cut off by stop()
            self._note(f"say exited {rc}")
        if then and rc == 0 and gen == self.gen:
            try:
                then()
            except Exception as e:
                self._note(f"speech callback failed: {e!r}")
