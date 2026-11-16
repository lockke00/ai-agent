# Pi Agent

A small terminal AI assistant for a Raspberry Pi Zero 2 W, using OpenRouter.

## Setup
    cp .env.example .env && chmod 600 .env
    nano .env            # paste your OpenRouter key

## Use
    ./agent.py                                   # chat
    ./agent.py "show the top memory processes"   # one-shot

Chat commands: `/status` (model, key credit, session tokens/cost, Pi health), `/clear`, `/model <openrouter-id>`, `/exit`. Ctrl-C cancels the current request.

## Safety
- Read-only commands (ps, df, du, find, grep, cat, ...) run automatically.
- Anything else (rm, writes, redirects `>`, sudo, apt, `find -delete`, `sed -i`, ...) asks y/N.
- File writes always ask, with a preview.
- The agent refuses to read its own `.env`.
