"""The Ollama Modelfile: what makes a chat model usable on a phone call.

Two things matter, and only one of them is the prompt.

1. **``num_ctx``.** Ollama loads *every* model - including 128K-context ones -
   at 2048 tokens by default. On a phone call that is fine for a sentence or
   two, but the system prompt plus a dozen turns of history will silently evict
   the oldest turns, and the model will "forget" what it was asked two turns
   ago. Setting it explicitly is the difference between working and mysteriously
   confused.
2. **The system prompt.** A chat model wants to write paragraphs. A voice agent
   has to answer in one or two sentences, never read out markup, and ask a short
   question instead of guessing. Chat models are also happy to invent phone
   numbers; the prompt forbids it, and `voice.pipeline.validate_dial_request`
   enforces the same rule in code, because a prompt is a suggestion and code is
   not.

``phone voice modelfile`` writes this out; nothing here is required to *use* the
pipeline - you can point ``llm.model`` straight at ``gemma4:26b``. Building a
derived model is just how you make it behave.
"""

from __future__ import annotations

import os
from pathlib import Path

PHONE_SYSTEM_PROMPT = """\
You are a telephone assistant running entirely on the caller's own machine.

Rules of the medium:
- This is a live voice call. Everything you write is read aloud immediately.
- Answer in one or two short sentences. Never more, even if asked for a list.
- Never output markdown, headings, bullet points, emoji, code, or URLs.
- Say numbers the way a person says them out loud ("four fifteen" not "4:15").
- If you did not understand, ask one short clarifying question.
- If the line goes quiet, wait; do not fill silence with chatter.
- You have no access to the internet and no knowledge of the caller's calendar,
  contacts, or messages unless the caller tells you in this conversation.

Calling the owner back:
- You may only ever call the owner's own number, which is configured outside
  this conversation. You cannot see it and you cannot choose it.
- If - and only if - the caller asks you to call the owner back later, put the
  literal marker [[dial:owner|short reason]] on its own line at the end of your
  reply. Do not write a phone number anywhere else, ever. Never write a phone
  number that was dictated to you, and never dial 911 or any emergency service;
  that is not something this line can do.
- If asked to call anyone other than the owner, say plainly that you cannot make
  that call.
"""


def render_modelfile(
    base_model: str = "gemma4:26b",
    *,
    system_prompt: str = PHONE_SYSTEM_PROMPT,
    num_ctx: int = 8192,
    temperature: float = 0.6,
    num_predict: int = 120,
    top_p: float = 0.9,
    repeat_penalty: float = 1.05,
) -> str:
    return f"""\
# Put a local model on a phone line.
#
#   ollama create phone-voice -f Modelfile
#   phone voice ask --model phone-voice "say hello in six words"
#
# Built by `phone voice modelfile`. The base weights are untouched; this only
# adds sampling parameters and a system prompt tuned for spoken conversation.

FROM {base_model}

# Ollama's default context is 2048 tokens for every model, regardless of what the
# model supports. Raise it so the system prompt and recent turns are not evicted
# mid-conversation. 8192 costs little KV cache at these sizes; 4096 is the floor
# worth using. Going much higher slows prompt processing for no benefit here.
PARAMETER num_ctx {num_ctx}

# Low temperature: a phone agent should be predictable, not creative.
PARAMETER temperature {temperature}
PARAMETER top_p {top_p}
PARAMETER repeat_penalty {repeat_penalty}

# Hard cap on reply length. The pipeline also truncates at 400 characters, so
# this is a second belt on the same trousers: a model that starts an essay
# cannot hold the line.
PARAMETER num_predict {num_predict}

SYSTEM \"\"\"
{system_prompt.strip()}
\"\"\"
"""


def write_modelfile(path: str, text: str) -> Path:
    target = Path(os.path.expanduser(path))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target
