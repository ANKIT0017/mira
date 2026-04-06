"""
Patch agent.py: wire _inject_buffer_refs_into_steps into the execute loop.
Run from MIRA-Windows directory: python fix_buffer_inject.py
"""
from pathlib import Path
import sys

AGENT = Path("agent/agent.py")
if not AGENT.exists():
    sys.exit(f"ERROR: {AGENT.resolve()} not found")

lines = AGENT.read_bytes().split(b"\n")

# Find the target block: "if not _step_rewound:" followed by completed.append
TARGET_START = b"            if not _step_rewound:  # don't log rewound steps; they'll re-run and log on success"
TARGET_ANCHOR = b"                completed.append({"

start_ln = None
anchor_ln = None
for i, line in enumerate(lines):
    stripped = line.rstrip(b"\r")
    if stripped == TARGET_START.rstrip(b"\r") and start_ln is None:
        start_ln = i
    if stripped == TARGET_ANCHOR.rstrip(b"\r") and start_ln is not None and anchor_ln is None:
        anchor_ln = i

if start_ln is None or anchor_ln is None:
    # Try fuzzy match
    for i, line in enumerate(lines):
        if b"_step_rewound" in line and b"don't log rewound" in line:
            start_ln = i
        if start_ln and b"completed.append({" in line:
            anchor_ln = i
            break

if start_ln is None or anchor_ln is None:
    print("ERROR: Could not locate target block. Dumping nearby lines...")
    for i, line in enumerate(lines):
        if b"completed.append" in line:
            print(f"  {i+1}: {line.rstrip()}")
    sys.exit(1)

# Find the end of the completed.append block (closing "})") and metrics line
end_ln = anchor_ln
for j in range(anchor_ln, min(anchor_ln + 20, len(lines))):
    if b"end_step" in lines[j]:
        end_ln = j
        break

print(f"Found block: lines {start_ln+1}–{end_ln+1}")
print(f"  {start_ln+1}: {lines[start_ln].rstrip()}")
print(f"  {anchor_ln+1}: {lines[anchor_ln].rstrip()}")
print(f"  {end_ln+1}: {lines[end_ln].rstrip()}")

# Detect line ending style
crlf = b"\r\n" in AGENT.read_bytes()[:2000]
EOL = b"\r\n" if crlf else b"\n"

INDENT12 = b"            "
INDENT16 = b"                "
INDENT20 = b"                    "

NEW_BLOCK = EOL.join([
    INDENT12 + b"if not _step_rewound:  # don't log rewound steps; they'll re-run and log on success",
    INDENT16 + b"if step_status == \"done\":",
    INDENT20 + b"self._maybe_update_handoff_payload(_step_tool, step_result or \"\")",
    INDENT16 + b"_completed_entry = {",
    INDENT20 + b"\"step\": step_desc,",
    INDENT20 + b"\"status\": step_status,",
    INDENT20 + b"\"result\": step_result if step_result else (last_error[:100] if last_error else \"failed\"),",
    INDENT20 + b"\"tool\": _step_tool,",
    INDENT20 + b"\"calls\": _step_calls,",
    INDENT20 + b"\"artifacts\": _step_artifacts,",
    INDENT20 + b"\"categories\": step_categories,",
    INDENT16 + b"}",
    INDENT16 + b"completed.append(_completed_entry)",
    INDENT16 + b"# Track step completion in metrics",
    INDENT16 + b"self._context_compiler.metrics.end_step(i, success=(step_status == \"done\"))",
    INDENT16 + b"# After a search/scraping step succeeds, annotate remaining steps with",
    INDENT16 + b"# the real BUFFER ID so downstream email/send steps know which buffer to read.",
    INDENT16 + b"if step_status == \"done\" and _step_tool.startswith((\"search.\", \"firecrawl.\")):",
    INDENT20 + b"_remaining_steps = steps[_step_idx:]",
    INDENT20 + b"if _remaining_steps:",
    INDENT20 + b"    steps[_step_idx:] = self._inject_buffer_refs_into_steps(",
    INDENT20 + b"        _remaining_steps, _completed_entry, self.buffer",
    INDENT20 + b"    )",
]) + EOL

# Replace lines start_ln through end_ln (inclusive)
new_lines = lines[:start_ln] + [NEW_BLOCK] + lines[end_ln + 1:]
result = b"\n".join(new_lines)

AGENT.write_bytes(result)
print(f"OK: patched lines {start_ln+1}–{end_ln+1}, file is now {len(result):,} bytes")

# Quick syntax check
import py_compile, tempfile, os
tmp = Path(tempfile.mktemp(suffix=".py"))
tmp.write_bytes(result)
try:
    py_compile.compile(str(tmp), doraise=True)
    print("Syntax: OK")
except py_compile.PyCompileError as e:
    print(f"Syntax ERROR: {e}")
finally:
    tmp.unlink(missing_ok=True)
