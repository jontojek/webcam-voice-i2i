"""Audio capture + faster-whisper transcription.

Architecture
------------
Two threads:

  _reader_thread  -- reads mic chunks continuously and classifies each chunk as
                     speech/silence by its own RMS (never blocks on Whisper)
  run() loop      -- a small state machine that accumulates chunks while speech
                     is happening and fires one Whisper call per *completed
                     utterance* (triggered by a real pause), instead of polling
                     a fixed-size ring buffer on a timer.

Why the rewrite
----------------
The previous version snapshotted a fixed 3s ring buffer every `transcribe_interval`
seconds and computed RMS over the *whole* window before deciding whether to skip
it as silence. Two bugs fell out of that:

  1. RMS-over-whole-window dilution -- a short, quiet-ish phrase surrounded by
     silence inside the same 3s window could average below `silence_threshold`
     and get skipped entirely, leaving `_latest_text` stale ("stuck").
  2. main.py's debounce required the *exact same string* to come back from two
     independent Whisper calls on overlapping, sliding windows 1.5s apart.
     Word-boundary drift between calls meant that rarely happened, so new
     prompts often never committed.

This version fixes both by detecting actual utterance boundaries (speech onset
/ pause) per-chunk, transcribing only complete utterances, and exposing a
monotonic sequence number so callers don't need string-equality at all.
"""

import threading
import time
from collections import deque

import numpy as np
import pyaudio
from faster_whisper import WhisperModel


class AudioTranscriber(threading.Thread):
    """Utterance-segmented transcriber. Call get_update() from any thread."""

    def __init__(
        self,
        model_size="base",
        device="auto",
        compute_type="int8",
        sample_rate=16000,
        chunk_duration_ms=500,
        channels=1,
        buffer_seconds=3,            # kept for config compat; no longer a hard window
        transcribe_interval=1.5,     # unused by the new loop; kept for config compat
        silence_threshold=0.01,
        min_silence_ms=600,          # pause length that ends an utterance
        min_speech_ms=150,           # speech length required to start an utterance
        max_utterance_ms=8000,       # force-flush long monologues so prompts still update
    ):
        super().__init__(daemon=True)
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_samples = int(sample_rate * chunk_duration_ms / 1000)
        self.chunk_duration_ms = chunk_duration_ms
        self.silence_threshold = silence_threshold

        self.min_speech_chunks = max(1, round(min_speech_ms / chunk_duration_ms))
        self.min_silence_chunks = max(1, round(min_silence_ms / chunk_duration_ms))
        self.max_utterance_chunks = max(1, round(max_utterance_ms / chunk_duration_ms))

        # Thread-safe handoff: reader thread classifies+queues chunks, run() consumes them.
        self._chunk_queue = deque()
        self._lock = threading.Lock()
        self._latest_text = ""
        self._utterance_seq = 0
        self._stop_event = threading.Event()

        # Load Whisper once at startup
        print("[AUDIO] Loading Whisper model '{}' on {} ({})...".format(model_size, device, compute_type))
        self._whisper = WhisperModel(model_size, device=device, compute_type=compute_type)
        print("[AUDIO] Whisper model ready.")

        # PyAudio stream
        self._pa = pyaudio.PyAudio()
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=sample_rate,
            input=True,
            frames_per_buffer=self.chunk_samples,
        )

    # -------------------------------------------------------------------- #
    # Internal: mic reader runs in its own thread so Whisper cannot starve it
    # -------------------------------------------------------------------- #
    def _reader_thread(self):
        while not self._stop_event.is_set():
            try:
                raw = self._stream.read(self.chunk_samples, exception_on_overflow=False)
            except OSError:
                time.sleep(0.05)
                continue
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(samples ** 2))) if samples.size else 0.0
            with self._lock:
                self._chunk_queue.append((samples, rms))

    # -------------------------------------------------------------------- #
    # Main thread: utterance segmentation + transcription
    # -------------------------------------------------------------------- #
    def run(self):
        reader = threading.Thread(target=self._reader_thread, daemon=True, name="mic-reader")
        reader.start()

        in_utterance = False
        utterance_chunks = []      # list of np.float32 chunk arrays for the current utterance
        speech_run = 0
        silence_run = 0
        preroll = deque(maxlen=1)  # last silent chunk, prepended on speech onset to avoid clipping

        while not self._stop_event.is_set():
            with self._lock:
                if not self._chunk_queue:
                    item = None
                else:
                    item = self._chunk_queue.popleft()

            if item is None:
                time.sleep(0.01)
                continue

            samples, rms = item
            is_speech = rms >= self.silence_threshold

            if not in_utterance:
                preroll.append(samples)
                if is_speech:
                    speech_run += 1
                    if speech_run >= self.min_speech_chunks:
                        # Speech onset confirmed -- start utterance, include pre-roll chunk
                        in_utterance = True
                        utterance_chunks = list(preroll) if len(preroll) else []
                        if not utterance_chunks or utterance_chunks[-1] is not samples:
                            utterance_chunks.append(samples)
                        silence_run = 0
                else:
                    speech_run = 0
                continue

            # -- in utterance --
            utterance_chunks.append(samples)
            if is_speech:
                silence_run = 0
            else:
                silence_run += 1

            force_flush = len(utterance_chunks) >= self.max_utterance_chunks
            if silence_run >= self.min_silence_chunks or force_flush:
                self._finalize_utterance(utterance_chunks)
                in_utterance = False
                utterance_chunks = []
                speech_run = 0
                silence_run = 0
                preroll.clear()

        reader.join(timeout=1.0)

    def _finalize_utterance(self, chunks):
        audio_seg = np.concatenate(chunks) if chunks else np.array([], dtype=np.float32)
        if audio_seg.size < self.chunk_samples // 2:
            return  # too short to be real speech

        segments, _ = self._whisper.transcribe(
            audio_seg,
            beam_size=1,
            best_of=1,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if text:
            with self._lock:
                self._latest_text = text
                self._utterance_seq += 1

    # -------------------------------------------------------------------- #
    # Public API
    # -------------------------------------------------------------------- #
    def get_latest_text(self):
        with self._lock:
            return self._latest_text

    def get_update(self):
        """Returns (utterance_seq, text). seq increments once per *completed*
        utterance, so callers can detect a new spoken phrase without needing
        the text to match exactly across calls."""
        with self._lock:
            return self._utterance_seq, self._latest_text

    def stop(self):
        self._stop_event.set()
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self._pa.terminate()
