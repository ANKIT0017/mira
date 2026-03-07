"""
Test to verify agent remembers tool execution results
"""
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from agent.agent import Agent

def test_tool_memory():
    print("Testing Agent Tool Execution Memory")
    print("=" * 60)
    
    agent = Agent()
    
    # Turn 1: Create a file
    print("\n[Turn 1] User: create a file called memory_test.txt")
    response1 = agent.handle("create a file called memory_test.txt")
    print(f"MIRA: {response1['reply']}")
    
    # Turn 2: Modify the SAME file (should recognize it exists)
    print("\n[Turn 2] User: now write 'Hello World' in memory_test.txt")
    response2 = agent.handle("now write 'Hello World' in memory_test.txt")
    print(f"MIRA: {response2['reply']}")
    
    # Turn 3: Modify again
    print("\n[Turn 3] User: add more content to memory_test.txt")
    response3 = agent.handle("add more content to memory_test.txt")
    print(f"MIRA: {response3['reply']}")
    
    # Check history
    print("\n\n" + "=" * 60)
    print(f"History stored: {len(agent.history)} turns")
    print("\nFull History (showing tool execution details):")
    for i, (user, assistant) in enumerate(agent.history, 1):
        print(f"\n  Turn {i}:")
        print(f"    User: {user}")
        print(f"    MIRA: {assistant}")
    
    print("\n✅ Test complete!")
    print("\nThe agent should now remember:")
    print("- Which files were created")
    print("- Where they are located") 
    print("- What operations were performed")

if __name__ == "__main__":
    test_tool_memory()
