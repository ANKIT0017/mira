"""
Test script to verify MIRA agent maintains conversation context
"""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from agent.agent import Agent

def test_context():
    print("Testing MIRA Agent Context & Memory")
    print("=" * 60)
    
    agent = Agent()
    
    # Turn 1
    print("\n[Turn 1] User: My name is John")
    response1 = agent.handle("My name is John")
    print(f"MIRA: {response1['reply']}")
    
    # Turn 2
    print("\n[Turn 2] User: I like programming")
    response2 = agent.handle("I like programming")
    print(f"MIRA: {response2['reply']}")
    
    # Turn 3 - Test if it remembers name
    print("\n[Turn 3] User: What's my name?")
    response3 = agent.handle("What's my name?")
    print(f"MIRA: {response3['reply']}")
    
    # Check history
    print(f"\n\nHistory stored: {len(agent.history)} turns")
    print("\nConversation History:")
    for i, (user, assistant) in enumerate(agent.history, 1):
        print(f"\n  Turn {i}:")
        print(f"    User: {user}")
        print(f"    MIRA: {assistant[:100]}...")
    
    # Test file creation in CREATEDBY-MIRA folder
    print("\n\n" + "=" * 60)
    print("Testing file creation in CREATEDBY-MIRA folder...")
    print("=" * 60)
    
    print("\n[Turn 4] User: Create a test file called context_test.txt with 'Hello from John'")
    response4 = agent.handle("Create a test file called context_test.txt with 'Hello from John'")
    print(f"MIRA: {response4['reply']}")
    
    print("\n✅ Test complete!")

if __name__ == "__main__":
    test_context()
