#!/usr/bin/env python3
"""Pi Agent - a small terminal AI assistant for a Raspberry Pi, backed by OpenRouter.

Usage:
    ./agent.py                      # interactive chat
    ./agent.py "check disk usage"   # one-shot

Read-only shell commands run automatically. Anything that writes, deletes,
installs, uses sudo, or isn't recognised asks for y/N approval first.
"""
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

AGENT_DIR = Path(__file__).resolve().parent
ENV_PATH = AGENT_DIR / ".env"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "anthropic/claude-sonnet-5.5"
MAX_TOOL_ROUNDS = 15
MAX_OUTPUT_CHARS = 6000
MAX_READ_BYTES = 100_000
MAX_HISTORY_CHARS = 80_000
CMD_TIMEOUT = 120

# Per-session counters shown by /status.
STATS = {
    "started": time.time(), "requests": 0, "prompt_tokens": 0,
    "completion_tokens": 0, "cost": 0.0, "commands_auto": 0,
    "commands_approved": 0, "commands_denied": 0, "files_written": 0,
}

# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #
TTY = sys.stdout.isatty()


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if TTY else text


def dim(t): return c(t, "2")
def bold(t): return c(t, "1")
def yellow(t): return c(t, "33")
def red(t): return c(t, "31")
def cyan(t): return c(t, "36")


def ask_approval(prompt):
    """Ask y/N. Non-interactive stdin means 'no'."""
    if not sys.stdin.isatty():
        print(yellow("  (no terminal for approval - denied)"))
        return False
    try:
        return input(yellow(f"  {prompt} [y/N] ")).strip().lower() in ("y", "yes")
    except EOFError:
        return False


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_env():
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))


# --------------------------------------------------------------------------- #
# Command safety classification
# --------------------------------------------------------------------------- #
SAFE_COMMANDS = {
    "ps", "free", "df", "du", "ls", "cat", "head", "tail", "wc", "grep", "egrep",
    "fgrep", "find", "stat", "file", "uname", "uptime", "whoami", "id", "hostname",
    "date", "which", "whereis", "echo", "pwd", "sort", "uniq", "cut", "awk", "tr",
    "column", "lsblk", "vmstat", "nproc", "lscpu", "vcgencmd", "pgrep", "basename",
    "dirname", "realpath", "readlink", "tree", "md5sum", "sha256sum", "diff",
    "journalctl", "last", "w", "who", "top", "sed", "printf", "true", "test",
}
# Flags that turn an otherwise read-only command into a writing/executing one.
RISKY_ARGS = {
    "find": {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls"},
    "sed": {"-i", "--in-place"},
    "journalctl": {"--vacuum-size", "--vacuum-time", "--vacuum-files", "--rotate", "--flush"},
}


def is_safe_command(cmd):
    """Return True only if every part of the command is known to be read-only."""
    # Allow the common harmless redirections, then reject anything else that
    # redirects, substitutes, or backgrounds.
    stripped = re.sub(r"\s2>\s*/dev/null|\s2>&1", " ", cmd)
    if re.search(r"[<>`]|\$\(|\$\{", stripped):
        return False
    if re.search(r"(?<![&])&(?![&])", stripped):  # lone & (background)
        return False
    if "system(" in stripped:  # awk system()
        return False
    for segment in re.split(r"\|\||&&|[|;\n]", stripped):
        segment = segment.strip()
        if not segment:
            continue
        try:
            tokens = shlex.split(segment)
        except ValueError:
            return False
        if not tokens:
            continue
        name = os.path.basename(tokens[0])
        if name not in SAFE_COMMANDS:
            return False
        risky = RISKY_ARGS.get(name, set())
        for tok in tokens[1:]:
            if tok in risky or any(tok.startswith(r + "=") for r in risky):
                return False
            if name == "sed" and re.match(r"^-[a-zA-Z]*i", tok):
                return False
        if name == "top" and "-b" not in tokens:
            return False  # interactive top would hang
    return True


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def truncate(text, limit=MAX_OUTPUT_CHARS):
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} chars truncated] ...\n\n{text[-half:]}"


def tool_run_command(command, reason=""):
    safe = is_safe_command(command)
    print(cyan(f"  $ {command}"))
    if not safe:
        if reason:
            print(dim(f"    why: {reason}"))
        if not ask_approval("Run this command?"):
            STATS["commands_denied"] += 1
            return "User denied permission to run this command."
        STATS["commands_approved"] += 1
    else:
        STATS["commands_auto"] += 1
    try:
        proc = subprocess.run(
            command, shell=True, executable="/bin/bash", capture_output=True,
            text=True, timeout=CMD_TIMEOUT, cwd=str(Path.home()),
        )
    except subprocess.TimeoutExpired:
        return f"Command timed out after {CMD_TIMEOUT}s."
    out = proc.stdout
    if proc.stderr:
        out += f"\n[stderr]\n{proc.stderr}"
    return truncate(f"[exit code {proc.returncode}]\n{out}".strip())


def tool_read_file(path):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    p = p.resolve()
    print(cyan(f"  read {p}"))
    if p == ENV_PATH:
        return "Refused: this file holds the agent's API key."
    if not p.is_file():
        return f"Not a file: {p}"
    data = p.read_bytes()[:MAX_READ_BYTES]
    if b"\x00" in data[:4096]:
        return f"{p} looks like a binary file."
    text = data.decode("utf-8", errors="replace")
    numbered = "\n".join(f"{i:4d}  {line}" for i, line in enumerate(text.splitlines(), 1))
    note = f"\n[truncated at {MAX_READ_BYTES} bytes]" if p.stat().st_size > MAX_READ_BYTES else ""
    return numbered + note


def tool_write_file(path, content, executable=False):
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.home() / p
    p = p.resolve()
    action = "Overwrite" if p.exists() else "Create"
    lines = content.splitlines()
    print(cyan(f"  {action.lower()} {p} ({len(lines)} lines{', executable' if executable else ''})"))
    for line in lines[:40]:
        print(dim(f"    | {line}"))
    if len(lines) > 40:
        print(dim(f"    | ... {len(lines) - 40} more lines"))
    if not ask_approval(f"{action} this file?"):
        return "User denied permission to write this file."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    if executable:
        p.chmod(0o755)
    STATS["files_written"] += 1
    return f"Wrote {len(content)} bytes to {p}."


TOOLS = {
    "run_command": tool_run_command,
    "read_file": tool_read_file,
    "write_file": tool_write_file,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a bash command on the Raspberry Pi (cwd = home directory) and return "
                "its output. Read-only commands run immediately; anything that modifies the "
                "system is shown to the user for approval first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The bash command."},
                    "reason": {"type": "string", "description": "One line on why, shown to the user when approval is needed."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file and return it with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file (always requires user approval).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "executable": {"type": "boolean", "description": "chmod 755 after writing."},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def system_prompt():
    return f"""You are Pi Agent, a concise system assistant running directly on a Raspberry Pi Zero 2 W.
Host: {platform.node()} | {platform.machine()} | Raspberry Pi OS (Debian bookworm) | Python {platform.python_version()}
User: {os.environ.get('USER', 'pi')} | Home: {Path.home()}
The Pi has only ~425MB RAM and a slow SD card: prefer lightweight commands and avoid scanning / when ~ will do.

Guidelines:
- Use tools to get real data; never guess at system state.
- Use non-interactive commands only (e.g. `top -b -n1`, never plain `top`, `less`, or editors).
- Use write_file to create scripts, then show how to run them.
- For cleanup: first list candidates with sizes using read-only commands, summarise them, then issue
  the deletion as a separate command. The user will be asked to approve it, and may decline.
- Never use sudo unless the task truly requires it, and say why.
- When explaining code, cite line numbers, explain the root cause, and give the corrected code.
- Keep answers short and terminal-friendly (plain text, minimal markdown)."""


# --------------------------------------------------------------------------- #
# Model loop
# --------------------------------------------------------------------------- #
def call_model(messages, model, api_key):
    resp = requests.post(
        API_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-Title": "Pi Agent",
        },
        json={"model": model, "messages": messages, "tools": TOOL_SCHEMAS, "max_tokens": 4096},
        timeout=180,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"OpenRouter error {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    if "choices" not in data:
        raise RuntimeError(f"Unexpected response: {json.dumps(data)[:500]}")
    usage = data.get("usage") or {}
    STATS["requests"] += 1
    STATS["prompt_tokens"] += usage.get("prompt_tokens", 0) or 0
    STATS["completion_tokens"] += usage.get("completion_tokens", 0) or 0
    STATS["cost"] += float(usage.get("cost", 0) or 0)
    return data["choices"][0]["message"]


# --------------------------------------------------------------------------- #
# /status
# --------------------------------------------------------------------------- #
def fmt_duration(seconds):
    seconds = int(seconds)
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    return (f"{d}d " if d else "") + (f"{h}h " if d or h else "") + f"{m}m"


def openrouter_key_info(api_key):
    """Return (info, error): credit/usage info for the key, or a short error string."""
    try:
        r = requests.get("https://openrouter.ai/api/v1/key",
                         headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    except requests.RequestException:
        return None, "couldn't reach OpenRouter"
    if r.status_code == 401:
        return None, "key rejected by OpenRouter (401) - check .env"
    if r.status_code != 200:
        return None, f"OpenRouter returned {r.status_code}"
    return (r.json().get("data") or {}), None


def show_status(model, api_key, messages):
    row = lambda label, value: print(f"  {dim(label.ljust(14))}{value}")

    print(bold("\nModel"))
    row("model", model)
    row("default", DEFAULT_MODEL + ("" if model == DEFAULT_MODEL else dim("  (changed)")))
    row("api key", f"sk-or-...{api_key[-4:]}" if len(api_key) > 8 else "set")
    info, err = openrouter_key_info(api_key)
    if info is not None:
        used = info.get("usage")
        limit = info.get("limit")
        remaining = info.get("limit_remaining")
        row("key usage", f"${used:.4f} total" if isinstance(used, (int, float)) else "n/a")
        if limit is not None:
            row("key limit", f"${limit:.2f}" + (f"  (${remaining:.2f} left)" if remaining is not None else ""))
        if info.get("is_free_tier"):
            row("tier", "free")
    else:
        row("key usage", red(err))

    print(bold("Session"))
    row("running", fmt_duration(time.time() - STATS["started"]))
    row("requests", STATS["requests"])
    row("tokens", f"{STATS['prompt_tokens']:,} in / {STATS['completion_tokens']:,} out")
    if STATS["cost"]:
        row("cost", f"${STATS['cost']:.4f}")
    history = len(json.dumps(messages))
    row("history", f"{len(messages) - 1} messages, {history // 1000}k / {MAX_HISTORY_CHARS // 1000}k chars")
    row("commands", f"{STATS['commands_auto']} auto, {STATS['commands_approved']} approved, "
                    f"{STATS['commands_denied']} denied")
    row("files written", STATS["files_written"])

    print(bold("Pi"))
    row("host", f"{platform.node()} ({platform.machine()})")
    try:
        up = float(Path("/proc/uptime").read_text().split()[0])
        row("uptime", fmt_duration(up))
    except OSError:
        pass
    try:
        row("load", " ".join(f"{x:.2f}" for x in os.getloadavg()))
    except OSError:
        pass
    try:
        mem = {k: int(v.split()[0]) for k, v in
               (line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        row("memory", f"{mem['MemAvailable'] // 1024}M free of {mem['MemTotal'] // 1024}M")
    except (OSError, KeyError, ValueError):
        pass
    du = shutil.disk_usage(Path.home())
    row("disk (/)", f"{du.free / 2**30:.1f}G free of {du.total / 2**30:.1f}G")
    try:
        temp = int(Path("/sys/class/thermal/thermal_zone0/temp").read_text()) / 1000
        row("cpu temp", f"{temp:.1f} C" + (yellow("  (hot)") if temp >= 70 else ""))
    except (OSError, ValueError):
        pass
    print()


def trim_history(messages):
    """Drop the oldest turns once history gets large, keeping tool-call pairs intact."""
    while len(json.dumps(messages)) > MAX_HISTORY_CHARS and len(messages) > 2:
        del messages[1]
        while len(messages) > 1 and messages[1]["role"] != "user":
            del messages[1]


def run_turn(messages, model, api_key):
    for _ in range(MAX_TOOL_ROUNDS):
        msg = call_model(messages, model, api_key)
        entry = {"role": "assistant", "content": msg.get("content") or ""}
        if msg.get("tool_calls"):
            entry["tool_calls"] = msg["tool_calls"]
        messages.append(entry)

        if msg.get("content"):
            print(f"\n{msg['content'].strip()}\n")
        if not msg.get("tool_calls"):
            return

        for call in msg["tool_calls"]:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = TOOLS[name](**args) if name in TOOLS else f"Unknown tool: {name}"
            except Exception as e:  # report tool failures back to the model
                result = f"Tool error: {type(e).__name__}: {e}"
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": result})
    print(yellow(f"(stopped after {MAX_TOOL_ROUNDS} tool rounds)"))


def main():
    load_env()
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    model = os.environ.get("OPENROUTER_MODEL", DEFAULT_MODEL).strip()
    if not api_key:
        sys.exit(red(f"OPENROUTER_API_KEY is not set. Add it to {ENV_PATH}"))

    messages = [{"role": "system", "content": system_prompt()}]

    def handle(prompt):
        checkpoint = len(messages)
        messages.append({"role": "user", "content": prompt})
        try:
            run_turn(messages, model, api_key)
        except KeyboardInterrupt:
            del messages[checkpoint:]
            print(yellow("\n(interrupted)"))
        except Exception as e:
            del messages[checkpoint:]
            print(red(f"Error: {e}"))
        trim_history(messages)

    if len(sys.argv) > 1:
        handle(" ".join(sys.argv[1:]))
        return

    print(bold("Pi Agent") + dim(f"  model: {model}   commands: /status /clear /model <id> /exit"))
    while True:
        try:
            prompt = input(bold("you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not prompt:
            continue
        if prompt in ("/exit", "/quit", "exit", "quit"):
            break
        if prompt == "/status":
            show_status(model, api_key, messages)
            continue
        if prompt == "/clear":
            del messages[1:]
            print(dim("(history cleared)"))
            continue
        if prompt.startswith("/model"):
            parts = prompt.split(maxsplit=1)
            if len(parts) == 2:
                model = parts[1]
            print(dim(f"(model: {model})"))
            continue
        handle(prompt)


if __name__ == "__main__":
    main()
