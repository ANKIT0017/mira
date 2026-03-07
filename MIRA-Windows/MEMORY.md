# MEMORY

This file stores persistent memory across runs.

memory.write: {{"content": "critical fact to remember", "tag": "optional-label"}}
memory.update: {{"id": <memory_note_id>, "content": "updated note", "mode": "replace|append"}}
OR update latest memory by tag: {{"tag":"project-x","content":"new detail","mode":"append"}} (edits MEMORY.md)

If I want to remember something, I write it here.
"Mental notes" do not survive session restarts. Files do.
When I make a mistake, document it so future me does not repeat it.
Text over memory state.
