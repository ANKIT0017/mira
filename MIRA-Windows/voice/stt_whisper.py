"""
Speech-to-text for MIRA voice mode.

Default path uses faster-whisper when installed, falling back to openai-whisper.
The recorder uses adaptive RMS-based endpoint detection so commands are not cut
off or recorded with long dead air before transcription.
"""
from __future__ import annotations

import queue
import re
import threading
from collections import deque
from typing import Optional

import numpy as np
import sounddevice as sd


class SpeechToText:
    def __init__(
        self,
        model_size: str = "base.en",
        backend: str = "auto",
        compute_type: str = "int8",
        device: str = "auto",
    ):
        """
        Initialize STT.

        Args:
            model_size: tiny.en, base.en, small.en, medium.en, or a faster-whisper model id.
            backend: auto, faster-whisper, or openai-whisper.
            compute_type: faster-whisper compute type, e.g. int8, int8_float16, float16.
            device: faster-whisper device, usually auto, cpu, or cuda.
        """
        self.model_size = model_size
        self.backend = backend
        self.compute_type = compute_type
        self.device = device
        self.sample_rate = 16000
        self.chunk_seconds = 0.10
        self.audio_queue: queue.Queue[np.ndarray] = queue.Queue()
        self.is_listening = False
        self._lock = threading.Lock()
        self.stream: Optional[sd.InputStream] = None

        self._load_model()

    def _load_model(self) -> None:
        requested = (self.backend or "auto").strip().lower()
        if requested in {"auto", "faster", "faster-whisper", "faster_whisper"}:
            try:
                from faster_whisper import WhisperModel

                print(
                    f"[STT] Loading faster-whisper model={self.model_size} "
                    f"device={self.device} compute_type={self.compute_type}"
                )
                self.model = WhisperModel(
                    self.model_size,
                    device=self.device,
                    compute_type=self.compute_type,
                )
                self.backend = "faster-whisper"
                return
            except Exception as exc:
                if requested != "auto":
                    raise
                print(f"[STT] faster-whisper unavailable, falling back to openai-whisper: {exc}")

        if requested in {"auto", "openai", "openai-whisper", "whisper"}:
            import whisper

            print(f"[STT] Loading openai-whisper model: {self.model_size}")
            self.model = whisper.load_model(self.model_size)
            self.backend = "openai-whisper"
            return

        raise ValueError("backend must be one of: auto, faster-whisper, openai-whisper")

    def _audio_callback(self, indata, frames, time, status) -> None:
        if status:
            print(f"[STT] Audio status: {status}")
        self.audio_queue.put(indata.copy())

    def start_listening(self) -> None:
        with self._lock:
            if self.is_listening:
                return
            self.is_listening = True

        self._drain_queue()
        print("[STT] Starting microphone...")
        self.stream = sd.InputStream(
            callback=self._audio_callback,
            dtype=np.float32,
            channels=1,
            samplerate=self.sample_rate,
            blocksize=int(self.sample_rate * self.chunk_seconds),
        )
        self.stream.start()

    def stop_listening(self) -> None:
        with self._lock:
            if not self.is_listening:
                return
            self.is_listening = False

        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        self._drain_queue()
        print("[STT] Microphone stopped")

    def _drain_queue(self) -> None:
        while not self.audio_queue.empty():
            try:
                self.audio_queue.get_nowait()
            except queue.Empty:
                break

    @staticmethod
    def _rms(chunk: np.ndarray) -> float:
        data = np.asarray(chunk, dtype=np.float32).reshape(-1)
        if data.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(data))))

    def _record_until_endpoint(
        self,
        silence_duration: float,
        max_duration: float,
        min_speech_duration: float,
    ) -> np.ndarray:
        chunk_seconds = self.chunk_seconds
        silence_chunks_required = max(1, int(silence_duration / chunk_seconds))
        max_chunks = max(1, int(max_duration / chunk_seconds))
        min_speech_chunks = max(1, int(min_speech_duration / chunk_seconds))
        calibration_chunks = max(3, int(0.5 / chunk_seconds))

        pre_roll = deque(maxlen=max(1, int(0.4 / chunk_seconds)))
        audio_chunks: list[np.ndarray] = []
        noise_rms: list[float] = []
        speech_chunks = 0
        silence_chunks = 0
        speech_started = False

        print("[STT] Listening... (speak now)")

        while len(audio_chunks) < max_chunks:
            try:
                chunk = self.audio_queue.get(timeout=0.25)
            except queue.Empty:
                continue

            chunk = np.asarray(chunk, dtype=np.float32)
            level = self._rms(chunk)

            if not speech_started and len(noise_rms) < calibration_chunks:
                noise_rms.append(level)

            noise_floor = float(np.median(noise_rms)) if noise_rms else 0.002
            # Adaptive threshold with a minimum floor. This is more reliable than
            # mean absolute amplitude when laptop mics have fans/room noise.
            speech_threshold = max(0.006, noise_floor * 3.0)
            is_speech = level >= speech_threshold

            if not speech_started:
                pre_roll.append(chunk)
                if is_speech:
                    speech_started = True
                    audio_chunks.extend(pre_roll)
                    speech_chunks = 1
                    silence_chunks = 0
                continue

            audio_chunks.append(chunk)
            if is_speech:
                speech_chunks += 1
                silence_chunks = 0
            else:
                silence_chunks += 1
                if speech_chunks >= min_speech_chunks and silence_chunks >= silence_chunks_required:
                    print("[STT] Endpoint detected, processing...")
                    break

        if not audio_chunks:
            return np.empty(0, dtype=np.float32)
        return np.concatenate(audio_chunks).astype(np.float32).reshape(-1)

    def _clean_transcript(self, text: str) -> str:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        # Common Whisper hallucination on silence/short noise.
        if text.lower() in {"thank you.", "thank you", "thanks for watching.", "you"}:
            return ""
        return text

    def _transcribe_audio(self, audio: np.ndarray) -> str:
        if self.backend == "faster-whisper":
            segments, info = self.model.transcribe(
                audio,
                language="en",
                beam_size=5,
                best_of=5,
                vad_filter=True,
                condition_on_previous_text=False,
                temperature=0.0,
            )
            text = " ".join(seg.text.strip() for seg in segments).strip()
            return self._clean_transcript(text)

        result = self.model.transcribe(
            audio,
            language="en",
            fp16=False,
            verbose=False,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        return self._clean_transcript(result.get("text", ""))

    def listen_for_speech(
        self,
        silence_duration: float = 1.0,
        max_duration: float = 15.0,
        min_speech_duration: float = 0.35,
    ) -> str:
        """
        Listen until speech ends, then transcribe.

        Args:
            silence_duration: Seconds of silence after speech to end recording.
            max_duration: Maximum recording duration.
            min_speech_duration: Ignore very short noise bursts below this duration.
        """
        self.start_listening()
        try:
            audio = self._record_until_endpoint(
                silence_duration=silence_duration,
                max_duration=max_duration,
                min_speech_duration=min_speech_duration,
            )
            if audio.size == 0:
                return ""
            duration = audio.size / self.sample_rate
            print(f"[STT] Transcribing {duration:.1f}s using {self.backend}...")
            text = self._transcribe_audio(audio)
            print(f"[STT] Transcribed: '{text}'")
            return text
        finally:
            self._drain_queue()

    def transcribe_file(self, audio_path: str) -> str:
        if self.backend == "faster-whisper":
            segments, info = self.model.transcribe(
                audio_path,
                language="en",
                beam_size=5,
                best_of=5,
                vad_filter=True,
                condition_on_previous_text=False,
                temperature=0.0,
            )
            return self._clean_transcript(" ".join(seg.text.strip() for seg in segments))
        result = self.model.transcribe(
            audio_path,
            language="en",
            fp16=False,
            condition_on_previous_text=False,
            temperature=0.0,
        )
        return self._clean_transcript(result.get("text", ""))


if __name__ == "__main__":
    stt = SpeechToText("base.en", backend="auto")
    print("Say something...")
    text = stt.listen_for_speech()
    print(f"You said: {text}")
    stt.stop_listening()
