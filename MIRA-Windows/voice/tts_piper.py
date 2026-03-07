"""
Text-to-Speech using Windows SAPI (fallback for piper-tts issues)
This is a simple, reliable TTS that works on all Windows systems
"""
import sounddevice as sd
import numpy as np
import threading
from typing import Optional, Generator
import win32com.client

class TextToSpeech:
    def __init__(self, voice_model: Optional[str] = None):
        """
        Initialize Windows SAPI TTS (reliable fallback)
        
        Args:
            voice_model: Ignored (for compatibility)
        """
        try:
            import win32com.client
            self.speaker = win32com.client.Dispatch("SAPI.SpVoice")
            self.is_speaking = False
            self._stop_event = threading.Event()
            self._playback_thread = None
            print("[TTS] Using Windows SAPI TTS")
        except ImportError:
            print("[TTS] ERROR: pywin32 not installed. Install with: pip install pywin32")
            self.speaker = None
    
    def speak(self, text: str, block: bool = True):
        """
        Synthesize and play speech
        
        Args:
            text: Text to speak
            block: If True, wait for speech to finish
        """
        if not text.strip() or not self.speaker:
            return
            
        self._stop_event.clear()
        self.is_speaking = True
        
        def _speak_thread():
            try:
                print(f"[TTS] Speaking: '{text[:50]}{'...' if len(text) > 50 else ''}'")
                if not self._stop_event.is_set():
                    self.speaker.Speak(text)
            except Exception as e:
                print(f"[TTS] Error: {e}")
            finally:
                self.is_speaking = False
        
        self._playback_thread = threading.Thread(target=_speak_thread, daemon=True)
        self._playback_thread.start()
        
        if block:
            self._playback_thread.join()
    
    def stop(self):
        """Stop current playback immediately"""
        if self.is_speaking and self.speaker:
            print("[TTS] Stopping playback...")
            self._stop_event.set()
            # SAPI doesn't have easy interrupt, just mark as stopped
            self.is_speaking = False
    
    def speak_streaming(self, text_generator: Generator[str, None, None]):
        """
        Speak text as it arrives from generator
        
        Args:
            text_generator: Generator yielding text chunks
        """
        sentence_buffer = ""
        
        for chunk in text_generator:
            if self._stop_event.is_set():
                break
                
            sentence_buffer += chunk
            
            # Check if we have a complete sentence
            if any(punct in sentence_buffer for punct in ['. ', '! ', '? ', '\n']):
                # Find the last sentence end
                for punct in ['. ', '! ', '? ']:
                    if punct in sentence_buffer:
                        idx = sentence_buffer.rfind(punct)
                        sentence = sentence_buffer[:idx + 1].strip()
                        sentence_buffer = sentence_buffer[idx + 1:].strip()
                        
                        if sentence:
                            self.speak(sentence, block=True)
                        break
        
        # Speak remaining text
        if sentence_buffer.strip() and not self._stop_event.is_set():
            self.speak(sentence_buffer.strip(), block=True)


if __name__ == "__main__":
    # Quick test
    tts = TextToSpeech()
    tts.speak("Hello! This is a test of the Windows text to speech system.")
    print("Done")
