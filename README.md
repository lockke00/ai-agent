# Pi Agent

A small terminal AI assistant for a Raspberry Pi Zero 2 W, using OpenRouter.

## Setup
    cp .env.example .env && chmod 600 .env
    nano .env            # paste your OpenRouter key

## Install as the `ai` command
    mkdir -p ~/.local/bin
    ln -sfn ~/repos/ai-agent/agent.py ~/.local/bin/ai
    # in ~/.zshrc:
    export PATH="$HOME/.local/bin:$PATH"
    alias ai="noglob ai"   # lets you type ? and * without quoting

## Use
    ai                                   # chat
    ai what is using the most memory?    # one-shot
    ./agent.py                                   # chat
    ./agent.py "show the top memory processes"   # one-shot

Chat commands: `/status` (model, key credit, session tokens/cost, Pi health), `/clear`, `/model <openrouter-id>`, `/exit`. Ctrl-C cancels the current request.

## Safety
- Read-only commands (ps, df, du, find, grep, cat, ...) run automatically.
- Anything else (rm, writes, redirects `>`, sudo, apt, `find -delete`, `sed -i`, ...) asks y/N.
- File writes always ask, with a preview.
- The agent refuses to read its own `.env`.
