"""
Speech-to-Text using OpenAI Whisper
Supports streaming transcription with VAD for sentence detection
"""
import whisper
import sounddevice as sd
import numpy as np
import queue
import threading
from typing import Generator, Optional

class SpeechToText:
    def __init__(self, model_size: str = "tiny.en"):
        """
        Initialize Whisper STT
        
        Args:
            model_size: tiny.en, base.en, small.en (tiny is fastest)
        """
        print(f"[STT] Loading Whisper model: {model_size}")
        self.model = whisper.load_model(model_size)
        self.sample_rate = 16000
        self.audio_queue = queue.Queue()
        self.is_listening = False
        self._lock = threading.Lock()
        
    def _audio_callback(self, indata, frames, time, status):
        """Callback for audio stream"""
        if status:
            print(f"[STT] Audio status: {status}")
        self.audio_queue.put(indata.copy())
    
    def start_listening(self):
        """Start microphone capture"""
        with self._lock:
            if self.is_listening:
                return
            self.is_listening = True
            
        print("[STT] Starting microphone...")
        self.stream = sd.InputStream(
            callback=self._audio_callback,
            dtype=np.float32,
            channels=1,
            samplerate=self.sample_rate,
            blocksize=int(self.sample_rate * 0.1)  # 100ms chunks
        )
        self.stream.start()
        
    def stop_listening(self):
        """Stop microphone capture"""
        with self._lock:
            if not self.is_listening:
                return
            self.is_listening = False
            
        if hasattr(self, 'stream'):
            self.stream.stop()
            self.stream.close()
        print("[STT] Microphone stopped")
    
    def listen_for_speech(self, silence_duration: float = 1.5, max_duration: float = 10.0) -> str:
        """
        Listen until silence is detected, then transcribe
        
        Args:
            silence_duration: Seconds of silence to end recording
            max_duration: Maximum recording duration
            
        Returns:
            Transcribed text
        """
        self.start_listening()
        
        audio_chunks = []
        silence_chunks = 0
        silence_threshold = int(silence_duration / 0.1)  # Convert to number of chunks
        max_chunks = int(max_duration / 0.1)
        
        print("[STT] Listening... (speak now)")
        
        try:
            while len(audio_chunks) < max_chunks:
                try:
                    chunk = self.audio_queue.get(timeout=0.2)
                    audio_chunks.append(chunk)
                    
                    # Simple VAD: check if chunk is mostly silence
                    if np.abs(chunk).mean() < 0.01:  # Adjust threshold as needed
                        silence_chunks += 1
                        if silence_chunks >= silence_threshold and len(audio_chunks) > 5:
                            print("[STT] Silence detected, processing...")
                            break
                    else:
                        silence_chunks = 0
                        
                except queue.Empty:
                    continue
                    
            if not audio_chunks:
                return ""
                
            # Concatenate audio and transcribe
            audio = np.concatenate(audio_chunks).flatten()
            
            print(f"[STT] Transcribing {len(audio)/self.sample_rate:.1f}s of audio...")
            result = self.model.transcribe(
                audio,
                language="en",
                fp16=False,  # Use fp32 for compatibility
                verbose=False
            )
            
            text = result['text'].strip()
            print(f"[STT] Transcribed: '{text}'")
            return text
            
        finally:
            # Clear queue
            while not self.audio_queue.empty():
                try:
                    self.audio_queue.get_nowait()
                except queue.Empty:
                    break

    def transcribe_file(self, audio_path: str) -> str:
        """Transcribe an audio file"""
        result = self.model.transcribe(audio_path, language="en")
        return result['text'].strip()


if __name__ == "__main__":
    # Quick test
    stt = SpeechToText("tiny.en")
    print("Say something...")
    text = stt.listen_for_speech()
    print(f"You said: {text}")
    stt.stop_listening()
