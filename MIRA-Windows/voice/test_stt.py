"""
Test script for STT (Speech-to-Text)
Run this to verify Whisper is working
"""

from voice.stt_whisper import SpeechToText

def test_stt():
    print("Testing Speech-to-Text...")
    print("=" * 60)
    
    stt = SpeechToText("tiny.en")
    
    print("\nSpeak for a few seconds (will auto-stop after silence)...")
    text = stt.listen_for_speech(silence_duration=2.0, max_duration=10.0)
    
    print(f"\nTranscribed: {text}")
    print("\nTest complete!")
    
    stt.stop_listening()

if __name__ == "__main__":
    test_stt()
