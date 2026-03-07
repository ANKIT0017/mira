"""
Test script for TTS (Text-to-Speech)
Run this to verify Piper is working
"""

from voice.tts_piper import TextToSpeech

def test_tts():
    print("Testing Text-to-Speech...")
    print("=" * 60)
    
    tts = TextToSpeech()
    
    test_phrases = [
        "Hello! This is a test of the Piper text to speech system.",
        "I can speak with natural intonation and rhythm.",
        "Testing complete!"
    ]
    
    for i, phrase in enumerate(test_phrases, 1):
        print(f"\n[{i}/{len(test_phrases)}] Speaking: {phrase}")
        tts.speak(phrase, block=True)
    
    print("\nTest complete!")

if __name__ == "__main__":
    test_tts()
