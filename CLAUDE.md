# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

Pi Agent: a single-file terminal AI assistant (`agent.py`) that runs on a
**Raspberry Pi Zero 2 W** (hostname `hackberrypizero`, armv7l / 32-bit,
~425MB RAM, Raspberry Pi OS bookworm, Python 3.11). It talks to an LLM through
OpenRouter's OpenAI-compatible chat completions API and gives the model three
tools: `run_command`, `read_file`, `write_file`.

It is installed as the `ai` command via a symlink
`~/.local/bin/ai -> ~/repos/ai-agent/agent.py`, so edits to `agent.py` are live
immediately. The user's login shell is **zsh** (`~/.zshrc` adds `~/.local/bin`
to PATH and `alias ai="noglob ai"`).

## Hard constraints

- **Dependencies: Python stdlib + `requests` only.** `requests` is installed
  system-wide. Do not add the `openai` SDK, `python-dotenv`, rich, etc. The Pi
  has very little RAM and slow storage; keep startup fast and memory small.
- **Keep it a single file** unless there is a strong reason to split.
- **Never read, print, or commit `.env`.** It holds `OPENROUTER_API_KEY` and
  `OPENROUTER_MODEL`, is chmod 600, and is gitignored. The agent's
  `read_file` tool also refuses it (`ENV_PATH` check); keep that behaviour.
- **Never print `git remote -v` or `.git/config`**: the remote URL may contain
  credentials.

## Safety model (do not weaken without being asked)

- `is_safe_command()` decides whether a shell command runs without approval.
  It is an **allowlist**: every pipeline/`&&`/`;` segment must start with a
  command in `SAFE_COMMANDS`, and must not use a flag in `RISKY_ARGS`
  (e.g. `find -delete/-exec`, `sed -i`, `journalctl --vacuum-*`).
- Any redirection (`>`, `<`), command substitution (`$(...)`, backticks,
  `${...}`), backgrounding `&`, or awk `system(` makes a command risky. Only
  `2>/dev/null` and `2>&1` are allowed.
- Unknown or unparseable commands are **risky by default** (ask y/N).
- `write_file` always asks, with a preview. Non-TTY stdin = deny.
- When adding a command to `SAFE_COMMANDS`, make sure it cannot write, delete,
  or execute other programs in any flag combination; otherwise add the
  dangerous flags to `RISKY_ARGS`.

## Code map (`agent.py`)

- Config constants at the top (`DEFAULT_MODEL`, limits, `CMD_TIMEOUT`).
- `STATS`: per-session counters shown by `/status`.
- `load_env()`: minimal `.env` parser (env vars already set take precedence).
- `is_safe_command()`, `SAFE_COMMANDS`, `RISKY_ARGS`: approval classifier.
- `tool_*` functions + `TOOLS` + `TOOL_SCHEMAS`: tool implementations and
  their JSON schemas (keep the two in sync).
- `system_prompt()`: tells the model about the Pi and the cleanup/approval flow.
- `call_model()`: POST to OpenRouter, records token usage/cost in `STATS`.
- `show_status()` / `openrouter_key_info()`: the `/status` command.
- `trim_history()`: drops oldest turns past `MAX_HISTORY_CHARS` without
  orphaning tool results.
- `run_turn()` / `main()`: tool loop and REPL (`/status`, `/clear`,
  `/model <id>`, `/exit`); one-shot mode when args are given.

## Testing

There is no test suite yet. Before committing, at minimum:

```bash
python3 -m py_compile agent.py
# classifier + tools, no API key needed (stdin not a TTY => risky commands are denied):
python3 -c "import agent; assert agent.is_safe_command('ps aux | head'); assert not agent.is_safe_command('rm -rf /tmp/x')" </dev/null
# REPL commands without spending credits (fake key => 401 is expected):
printf '/status\n/exit\n' | OPENROUTER_API_KEY=sk-or-fake ./agent.py
```

Only run real prompts against OpenRouter when the user asks: they cost money.
Model IDs can be checked for free with
`GET https://openrouter.ai/api/v1/models`.

## Conventions

- Branch: `main`. Remote: `origin` on GitHub (`lockke00/ai-agent`).
- Commit messages: short imperative subject, a body explaining why.
- Keep `README.md` in sync when user-facing commands or setup change.
- Terminal output: plain text, ASCII only (the degree sign rendered as
  mojibake over SSH, so e.g. use `44.0 C`).
