
import asyncio
import websockets
import json

async def test_agent():
    uri = "ws://localhost:8765"
    async with websockets.connect(uri) as ws:
        print("Connected to WebSocket")
        # Send a simple message to trigger the agent
        payload = {
            "action": "agent_invoke",
            "payload": {"input": "Hello MIRA, do you know what time it is?"}
        }
        print(f"Sending: {payload}")
        await ws.send(json.dumps(payload))
        
        while True:
            try:
                res = await asyncio.wait_for(ws.recv(), timeout=20.0)
                print(f"Received: {res}")
                data = json.loads(res)
                # If we get a final reply or error, break
                if data.get("result", {}).get("reply") or data.get("error"):
                    break
            except asyncio.TimeoutError:
                print("Timeout waiting for response")
                break

if __name__ == "__main__":
    asyncio.run(test_agent())
