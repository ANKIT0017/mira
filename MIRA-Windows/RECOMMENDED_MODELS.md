# Recommended Models for MIRA

## Current Issue with qwen2.5:3b

You're currently using `qwen2.5:3b` (3 billion parameters), which is experiencing hallucination issues:
- Invents follow-up user messages
- Claims actions were taken when they weren't
- Generates fake conversation continuations

**This is a model capacity problem.** 3B is too small for reliable tool calling and conversation management.

## Recommended Upgrades (in order of preference)

### Best Performance
1. **`mistral:7b`** ⭐ RECOMMENDED
   - 7 billion parameters
   - Excellent instruction following
   - Very good at tool calling
   - Fast on modern hardware
   - **Install:** `ollama pull mistral:7b`

2. **`llama3.2:8b`**
   - 8 billion parameters  
   - Great tool calling accuracy
   - Meta's latest architecture
   - **Install:** `ollama pull llama3.2:8b`

### Good Balance
3. **`qwen2.5:7b`**
   - 7 billion parameters
   - Better than 3b version
   - **Install:** `ollama pull qwen2.5:7b`

4. **`gemma2:9b`**
   - 9 billion parameters
   - Google's model
   - **Install:** `ollama pull gemma2:9b`

### Minimum Acceptable
5. **`phi3:mini`** (3.8B)
   - Still small but better instruction tuning than qwen2.5:3b
   - **Install:** `ollama pull phi3:mini`

## How to Change Model

1. Pull the new model:
   ```bash
   ollama pull mistral:7b
   ```

2. Edit `agent/model_adapter.py` line 18:
   ```python
   # Change from:
   model: str = "qwen2.5:3b"
   
   # To:
   model: str = "mistral:7b"
   ```

3. Restart the servers

## Why Bigger Models Work Better

- **Tool Calling**: Requires understanding context and exact JSON formatting
- **Conversation Memory**: Needs to track what was said vs what wasn't
- **Following Instructions**: Small models struggle with complex system prompts
- **Less Hallucination**: Bigger models have more capacity to "think" accurately

**Bottom line**: Upgrade to at least 7B parameters for stable, reliable MIRA performance.
