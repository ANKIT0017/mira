"""
Gemini Live API Voice Integration for Autonomous Scheduler
Uses Gemini 2.5 Flash Native Audio Dialog for natural voice notifications
"""
import os
import json
import base64
import asyncio
import websockets
import pyaudio
import threading
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

class GeminiLiveVoice:
    """
    Gemini Live API integration for real-time voice conversations.
    Handles bidirectional audio streaming over WebSocket.
    """
    
    def __init__(self, api_key: str = None, model: str = "gemini-2.0-flash-live-001"):
        """
        Initialize Gemini Live Voice client.
        
        Args:
            api_key: Gemini API key (defaults to GEMINI_API_KEY env var)
            model: Model name (gemini-2.0-flash-live-001 is the official Live API model)
        """
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        if not self.api_key:
            raise ValueError("GEMINI_API_KEY not found in environment")
        
        self.model = model
        # Updated WebSocket URL for v1alpha API
        self.ws_url = f"wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1alpha.GenerativeService.BidiGenerateContent?key={self.api_key}"
        
        # Audio configuration (Gemini Live expects 16kHz PCM)
        self.SAMPLE_RATE = 16000
        self.CHUNK_SIZE = 512
        self.FORMAT = pyaudio.paInt16
        self.CHANNELS = 1
        
        self.audio = pyaudio.PyAudio()
        self.is_listening = False
        self.conversation_active = False
        
        print(f"[GEMINI LIVE] Initialized with model: {model}")
    
    async def _send_setup_message(self, ws):
        """Send initial setup message to configure the Live API session."""
        setup_msg = {
            "setup": {
                "model": f"models/{self.model}",
                "generation_config": {
                    "response_modalities": ["AUDIO"],  # Request audio responses
                    "speech_config": {
                        "voice_config": {
                            "prebuilt_voice_config": {
                                "voice_name": "Puck"  # Natural male voice
                            }
                        }
                    }
                }
            }
        }
        await ws.send(json.dumps(setup_msg))
        print("[GEMINI LIVE] Sent setup message")
    
    async def _send_text_message(self, ws, text: str):
        """Send a text message through the WebSocket."""
        message = {
            "client_content": {
                "turns": [{
                    "role": "user",
                    "parts": [{"text": text}]
                }],
                "turn_complete": True
            }
        }
        await ws.send(json.dumps(message))
        print(f"[GEMINI LIVE] Sent text: {text[:50]}...")
    
    async def _receive_audio_response(self, ws):
        """
        Receive audio from Gemini and play it through speakers.
        Returns the text transcript as well.
        """
        stream = self.audio.open(
            format=self.FORMAT,
            channels=self.CHANNELS,
            rate=24000,  # Gemini outputs 24kHz audio
            output=True
        )
        
        transcript = []
        
        try:
            while True:
                response = await ws.recv()
                data = json.loads(response)
                
                # Check for audio data
                if "serverContent" in data:
                    server_content = data["serverContent"]
                    
                    if "modelTurn" in server_content:
                        for part in server_content["modelTurn"].get("parts", []):
                            # Play audio
                            if "inlineData" in part:
                                audio_bytes = base64.b64decode(part["inlineData"]["data"])
                                stream.write(audio_bytes)
                            
                            # Collect transcript
                            if "text" in part:
                                transcript.append(part["text"])
                    
                    # Check if turn is complete
                    if server_content.get("turnComplete"):
                        print("[GEMINI LIVE] Turn complete")
                        break
                        
        finally:
            stream.stop_stream()
            stream.close()
        
        return " ".join(transcript)
    
    async def speak_notification(self, event: dict, allow_conversation: bool = True):
        """
        Speak an event notification using Gemini Live.
        
        Args:
            event: Calendar event dict with title, time, notification_message
            allow_conversation: If True, keeps session open for user response (default: True)
        
        Returns:
            str: Response transcript if conversation occurred
        """
        notification_text = event.get("notification_message") or f"Reminder: {event.get('title')}"
        
        print(f"[GEMINI LIVE] Starting notification: {notification_text}")
        
        try:
            async with websockets.connect(self.ws_url) as ws:
                # Setup session
                await self._send_setup_message(ws)
                
                # Wait for setup confirmation
                setup_response = await ws.recv()
                print(f"[GEMINI LIVE] Setup response: {setup_response[:100]}...")
                
                # Send notification as conversational prompt
                prompt = f"{notification_text}\n\nAsk the user how they're feeling or if they need anything."
                await self._send_text_message(ws, prompt)
                
                # Receive and play audio response
                transcript = await self._receive_audio_response(ws)
                print(f"[GEMINI LIVE] Gemini said: {transcript}")
                
                if allow_conversation:
                    # Keep session open for 30 seconds to allow user response
                    print("[GEMINI LIVE] Listening for user response (30s timeout)...")
                    # TODO: Implement microphone input streaming
                    # For now, just close after notification
                    await asyncio.sleep(2)
                
                return transcript
                
        except Exception as e:
            print(f"[GEMINI LIVE] Error: {e}")
            # Fallback to Windows SAPI if Gemini fails
            import win32com.client
            speaker = win32com.client.Dispatch("SAPI.SpVoice")
            speaker.Speak(notification_text)
            return notification_text
    
    def speak_notification_sync(self, event: dict):
        """Synchronous wrapper for speak_notification."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.speak_notification(event))
        finally:
            loop.close()
    
    def cleanup(self):
        """Clean up audio resources."""
        self.audio.terminate()
        print("[GEMINI LIVE] Cleaned up resources")


# Simple test function
async def test_gemini_live():
    """Test Gemini Live with a sample notification."""
    voice = GeminiLiveVoice()
    
    test_event = {
        "title": "Wake up",
        "time": "06:00",
        "notification_message": "Good morning! It's 6 AM, time to wake up!"
    }
    
    await voice.speak_notification(test_event, allow_conversation=False)
    voice.cleanup()


if __name__ == "__main__":
    print("=" * 60)
    print("Testing Gemini Live API")
    print("=" * 60)
    asyncio.run(test_gemini_live())
