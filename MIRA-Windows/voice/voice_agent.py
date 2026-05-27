"""
Voice Agent - Main conversational loop
Integrates STT → Agent → TTS with barge-in support
"""

from voice.stt_whisper import SpeechToText
from voice.tts_piper import TextToSpeech
from agent.agent import Agent
import threading
import time

class VoiceAgent:
    def __init__(
        self,
        stt_model: str = "base.en",
        tts_voice: str = None,
        stt_backend: str = "auto",
        stt_compute_type: str = "int8",
    ):
        """
        Initialize voice agent
        
        Args:
            stt_model: Whisper model size (tiny.en, base.en, small.en)
            tts_voice: Path to Piper voice model (optional)
            stt_backend: auto, faster-whisper, or openai-whisper
            stt_compute_type: faster-whisper compute type
        """
        print("=" * 60)
        print("MIRA Voice Assistant Initializing...")
        print("=" * 60)
        
        print("\n[1/3] Loading Speech-to-Text...")
        self.stt = SpeechToText(
            model_size=stt_model,
            backend=stt_backend,
            compute_type=stt_compute_type,
        )
        
        print("\n[2/3] Loading Text-to-Speech...")
        self.tts = TextToSpeech(voice_model=tts_voice)
        
        print("\n[3/3] Loading AI Agent...")
        self.agent = Agent()
        
        self.running = False
        self._barge_in_detected = False
        
        print("\n" + "=" * 60)
        print("MIRA Voice Assistant Ready!")
        print("=" * 60)
    
    def _check_for_barge_in(self):
        """Background thread to detect user interruption"""
        # Start listening for new speech while agent is speaking
        # If detected, set flag to stop current response
        # This is a simple implementation - can be enhanced with better VAD
        pass
    
    def run(self):
        """Main voice interaction loop"""
        self.running = True
        
        print("\n🎤 Voice mode active. Say something to start (or Ctrl+C to quit)\n")
        
        try:
            while self.running:
                # 1. Listen for user speech
                user_text = self.stt.listen_for_speech(
                    silence_duration=1.0,
                    max_duration=15.0
                )
                
                if not user_text:
                    continue
                
                print(f"\n👤 You: {user_text}")
                
                # Check for exit commands
                if any(cmd in user_text.lower() for cmd in ["exit", "quit", "stop", "goodbye"]):
                    self.tts.speak("Goodbye!", block=True)
                    break
                
                # 2. Stop any ongoing speech (barge-in)
                self.tts.stop()
                
                # 3. Get agent response
                print("🤖 MIRA: ", end="", flush=True)
                
                try:
                    response = self.agent.handle(user_text)
                    response_text = response.get("reply", "I didn't understand that.")
                    
                    # Print response as it's generated
                    print(response_text)
                    
                    # 4. Speak response
                    self.tts.speak(response_text, block=True)
                    
                except Exception as e:
                    error_msg = f"Sorry, I encountered an error: {str(e)}"
                    print(error_msg)
                    self.tts.speak(error_msg, block=True)
                
                print()  # Blank line for readability
                
        except KeyboardInterrupt:
            print("\n\nStopping voice agent...")
        finally:
            self.cleanup()
    
    def run_streaming(self):
        """
        Enhanced mode with streaming LLM responses
        NOTE: Requires agent.handle_streaming() to be implemented
        """
        self.running = True
        
        print("\n🎤 Voice mode active (streaming). Say something to start (or Ctrl+C to quit)\n")
        
        try:
            while self.running:
                # Listen for speech
                user_text = self.stt.listen_for_speech()
                
                if not user_text:
                    continue
                
                print(f"\n👤 You: {user_text}")
                
                # Check for exit
                if any(cmd in user_text.lower() for cmd in ["exit", "quit", "stop"]):
                    self.tts.speak("Goodbye!", block=True)
                    break
                
                # Stop ongoing speech
                self.tts.stop()
                
                print("🤖 MIRA: ", end="", flush=True)
                
                try:
                    # Stream response (if implemented)
                    if hasattr(self.agent, 'handle_streaming'):
                        token_stream = self.agent.handle_streaming(user_text)
                        self.tts.speak_streaming(token_stream)
                    else:
                        # Fallback to non-streaming
                        response = self.agent.handle(user_text)
                        response_text = response.get("reply", "")
                        print(response_text)
                        self.tts.speak(response_text, block=True)
                        
                except Exception as e:
                    error_msg = f"Error: {str(e)}"
                    print(error_msg)
                    self.tts.speak(error_msg, block=True)
                
                print()
                
        except KeyboardInterrupt:
            print("\n\nStopping voice agent...")
        finally:
            self.cleanup()
    
    def cleanup(self):
        """Clean up resources"""
        self.running = False
        self.stt.stop_listening()
        self.tts.stop()
        print("Voice agent stopped.")


def main():
    """Entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description="MIRA Voice Assistant")
    parser.add_argument(
        "--stt-model",
        default="base.en",
        choices=["tiny.en", "base.en", "small.en", "medium.en"],
        help="Whisper model size"
    )
    parser.add_argument(
        "--stt-backend",
        default="auto",
        choices=["auto", "faster-whisper", "openai-whisper"],
        help="STT backend. auto prefers faster-whisper and falls back to openai-whisper."
    )
    parser.add_argument(
        "--stt-compute-type",
        default="int8",
        help="faster-whisper compute type: int8, int8_float16, float16, float32"
    )
    parser.add_argument(
        "--tts-voice",
        default=None,
        help="Path to Piper voice model (.onnx)"
    )
    parser.add_argument(
        "--streaming",
        action="store_true",
        help="Use streaming mode (experimental)"
    )
    
    args = parser.parse_args()
    
    # Create and run voice agent
    voice_agent = VoiceAgent(
        stt_model=args.stt_model,
        tts_voice=args.tts_voice,
        stt_backend=args.stt_backend,
        stt_compute_type=args.stt_compute_type,
    )
    
    if args.streaming:
        voice_agent.run_streaming()
    else:
        voice_agent.run()


if __name__ == "__main__":
    main()
