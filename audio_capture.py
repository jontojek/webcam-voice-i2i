"""Audio capture + faster-whisper transcription.

Architecture
------------
Two threads share a rolling deque:

  _reader_thread  -- reads mic chunks continuously into _audio_buffer (never blocks on Whisper)
  run() loop      -- wakes every `transcribe_interval` seconds, snapshots the buffer,
                     checks RMS energy (skips silence), then runs Whisper

This prevents Whisper from starving the mic read loop and dropping audio chunks.
"""

import threading
import time
from collections import deque

import numpy as np
import pyaudio
from faster_whisper import WhisperModel


class AudioTranscriber(threading.Thread):
    """Rolling-buffer transcriber.  Call get_latest_text() from any thread."""

    def __init__(
        self,
        model_size="base",
        device="auto",
        compute_type="int8",
        sample_rate=16000,
        chunk_duration_ms=500,
        channels=1,
        buffer_seconds=3,
        transcribe_interval=1.0,
        silence_threshold=0.01,
    ):
        super().__init__(daemon=True)
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_samples = int(sample_rate * chunk_duration_ms / 1000)
        self.buffer_samples = int(sample_rate * buffer_seconds)
        self.transcribe_interval = transcribe_interval
        self.silence_threshold = silence_threshold

        # Thread-safe rolling audio buffer
        self._audio_buffer = deque(maxlen=self.buffer_samples)
        self._lock = threading.Lock()
        self._latest_text = ""
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
            with self._lock:
                self._audio_buffer.extend(samples)

    # -------------------------------------------------------------------- #
    # Main thread: transcription loop
    # -------------------------------------------------------------------- #
    def run(self):
        reader = threading.Thread(target=self._reader_thread, daemon=True, name="mic-reader")
        reader.start()

        while not self._stop_event.is_set():
            time.sleep(self.transcribe_interval)

            # Snapshot buffer under lock, then release before calling Whisper
            with self._lock:
                if len(self._audio_buffer) < self.chunk_samples:
                    continue
                audio_seg = np.array(self._audio_buffer, dtype=np.float32)

            # Skip silence -- saves CPU and avoids Whisper hallucinations on dead air
            rms = float(np.sqrt(np.mean(audio_seg ** 2)))
            if rms < self.silence_threshold:
                continue

            segments, _ = self._whisper.transcribe(
                audio_seg,
                beam_size=1,
                best_of=1,
                temperature=0.0,
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            if text:
                with self._lock:
                    self._latest_text = text

        reader.join(timeout=1.0)

    # -------------------------------------------------------------------- #
    # Public API
    # -------------------------------------------------------------------- #
    def get_latest_text(self):
        with self._lock:
            return self._latest_text

    def stop(self):
        self._stop_event.set()
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        self._pa.terminate()
