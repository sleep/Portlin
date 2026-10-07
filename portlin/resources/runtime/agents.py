# Part of portlin. Copyright (C) 2026 the portlin authors.
# Licensed under the GNU General Public License, version 3 or later.
# See <https://www.gnu.org/licenses/gpl-3.0.html>.
"""Which AI agents are running on this machine, and what each one is doing.

Two layers, because they fail separately. The first is the process table:
any agent the Software app can install shows up there by the name of its
binary, with its working directory, memory and the child processes it has
spawned, and that much is true of an agent whose files this module has never
seen. The second is what the agents that keep a transcript on disk say about
themselves. Claude Code writes one JSON line per message under ~/.claude,
Codex one per event under ~/.codex, and OpenCode keeps a SQLite database;
each carries the model, the tokens spent and what the last turn was doing,
which is how a kiosk screen can say "running pytest in portlin, 41% of the
context used" about a terminal it cannot see.

Everything here is read-only and reads only the invoking user's own files.
Nothing is sent anywhere and no agent is asked anything: a transcript is
read from where it is written, the way `tail -f` would.

Transcripts grow to tens of megabytes. Each one is parsed once and then only
from where the last read stopped, with the file's identity checked so that a
truncated or replaced file starts over rather than continuing mid-line.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

# The agents, by the binary each one runs as. Matched against the first
# token of a command line and the one after it, because several of these
# are a script run by node or python and the interpreter comes first.
AGENTS = {
    "claude": "Claude Code",
    "codex": "Codex CLI",
    "opencode": "OpenCode",
    "hermes": "Hermes Agent",
    "openclaw": "OpenClaw",
    "goose": "goose",
    "kimi": "Kimi Code",
    "gemini": "Gemini CLI",
    "aider": "aider",
}

# The catalog's own category for these, so "installed" agrees with Software.
CATALOG_CATEGORY = "AI tools"

# Claude Code's context window by model, with the 1M variant told apart by
# the suffix the model name carries. A transcript does not record the
# window, only the model.
CONTEXT_WINDOW = 200_000
CONTEXT_WINDOW_1M = 1_000_000

# A subagent transcript written to in the last this-many seconds is working.
SUBAGENT_ACTIVE_SECONDS = 15

# Sessions nothing has written to for longer than this are not shown unless
# a process still holds them: a closed terminal is not a running agent.
RECENT_SECONDS = 120

# Prompts and tool arguments are cut to this many characters for a card.
SHORT = 100

# Claude Code tells only its status-line command about its rate limits.
# abtop's `--setup` installs one that writes them here, and that file is
# the one place on disk they can be read from.
CLAUDE_LIMITS_FILE = ".claude/abtop-rate-limits.json"
CLAUDE_LIMIT_WINDOWS = (("five_hour", "primary", 300), ("seven_day", "secondary", 10080))


@dataclass(frozen=True)
class AgentProcess:
    pid: int
    ppid: int
    agent: str
    command: str
    cwd: str
    rss_bytes: int
    cpu_seconds: float
    started_at: float
    children: tuple[str, ...] = ()


@dataclass
class AgentSession:
    """One agent's conversation, as far as its own files describe it."""

    agent: str
    pid: int
    project: str
    cwd: str
    title: str = ""
    model: str = ""
    status: str = ""
    task: str = ""
    context_tokens: int = 0
    context_window: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_create: int = 0
    turns: int = 0
    started_at: float = 0.0
    last_at: float = 0.0
    git_branch: str = ""
    version: str = ""
    limits: dict | None = None
    subagents: int = 0
    subagents_active: int = 0

    @property
    def context_percent(self) -> float | None:
        if self.context_window <= 0 or self.context_tokens <= 0:
            return None
        return min(100.0, 100.0 * self.context_tokens / self.context_window)


# -- processes -----------------------------------------------------------------


def _binary(token: str) -> str:
    name = os.path.basename(token)
    for suffix in (".exe", ".js", ".mjs", ".py"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name


def match_agent(argv: list[str]) -> str | None:
    """The agent a command line runs, or None.

    The first two tokens are looked at: "claude" alone, or "node .../cli.js"
    under a directory named for the agent, which is how npm installs them.
    A path of the form <...>/<agent>/versions/<file> is the auto-updater's
    layout and names the agent by its directory rather than its file.
    """
    for token in argv[:2]:
        if not token:
            continue
        if _binary(token) in AGENTS:
            return _binary(token)
        parts = Path(token).parts
        for index, part in enumerate(parts[:-1]):
            if part in AGENTS and parts[index + 1] == "versions":
                return part
    return None


def parse_proc_stat_line(text: str) -> dict | None:
    """The fields of /proc/<pid>/stat this needs, found after the last ")".

    comm is in parentheses and may itself contain spaces and parentheses, so
    the fields are counted from the end of it rather than from the start.
    """
    start = text.find("(")
    end = text.rfind(")")
    if start < 0 or end < 0:
        return None
    fields = text[end + 1:].split()
    if len(fields) < 20:
        return None
    try:
        return {
            "comm": text[start + 1:end],
            "state": fields[0],
            "ppid": int(fields[1]),
            "utime": int(fields[11]),
            "stime": int(fields[12]),
            "starttime": int(fields[19]),
        }
    except ValueError:
        return None


def _read(path: Path) -> str:
    try:
        return path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""


def read_processes(
    root: Path = Path("/"),
    *,
    self_pid: int | None = None,
    clock_ticks: int | None = None,
    page_size: int | None = None,
    now: float | None = None,
) -> list[AgentProcess]:
    """Every agent process in the process table, with its children's names."""
    self_pid = os.getpid() if self_pid is None else self_pid
    clock_ticks = clock_ticks or os.sysconf("SC_CLK_TCK")
    page_size = page_size or os.sysconf("SC_PAGE_SIZE")
    now = time.time() if now is None else now
    uptime_fields = _read(root / "proc/uptime").split()
    try:
        boot_time = now - float(uptime_fields[0])
    except (IndexError, ValueError):
        boot_time = now
    proc = root / "proc"
    try:
        pids = sorted(int(entry.name) for entry in proc.iterdir() if entry.name.isdigit())
    except OSError:
        return []
    stats: dict[int, dict] = {}
    agents: dict[int, tuple[str, list[str]]] = {}
    for pid in pids:
        if pid == self_pid:
            continue
        stat = parse_proc_stat_line(_read(proc / str(pid) / "stat"))
        if stat is None:
            continue
        stats[pid] = stat
        argv = _read(proc / str(pid) / "cmdline").split("\0")
        agent = match_agent(argv)
        if agent:
            agents[pid] = (agent, argv)
    children: dict[int, list[str]] = {}
    for pid, stat in stats.items():
        children.setdefault(stat["ppid"], []).append(stat["comm"])
    found = []
    for pid, (agent, argv) in agents.items():
        stat = stats[pid]
        # A child that is itself an agent process (the same binary forked
        # for a subagent, or a node helper) is still a child; the rows the
        # HUD draws come from the transcripts, so this is only the count.
        statm = _read(proc / str(pid) / "statm").split()
        rss = int(statm[1]) * page_size if len(statm) > 1 and statm[1].isdigit() else 0
        try:
            cwd = os.readlink(proc / str(pid) / "cwd")
        except OSError:
            cwd = ""
        found.append(AgentProcess(
            pid=pid,
            ppid=stat["ppid"],
            agent=agent,
            command=" ".join(_binary(token) for token in argv[:2] if token),
            cwd=cwd,
            rss_bytes=rss,
            cpu_seconds=(stat["utime"] + stat["stime"]) / clock_ticks,
            started_at=boot_time + stat["starttime"] / clock_ticks,
            children=tuple(sorted(children.get(pid, []))),
        ))
    return found


# -- incremental transcript reading --------------------------------------------


def tail_lines(path: Path, entry: dict) -> list[str]:
    """Lines added to ``path`` since the last call with the same ``entry``.

    ``entry`` holds the offset and the file's identity. A file that shrank
    or was replaced is read from the start again, and a partial last line
    is kept back until its newline arrives, so a JSON object being written
    is never parsed half way through.
    """
    try:
        stat = path.stat()
    except OSError:
        return []
    identity = (stat.st_dev, stat.st_ino)
    if entry.get("identity") != identity or stat.st_size < entry.get("offset", 0):
        entry.clear()
        entry["identity"] = identity
        entry["offset"] = 0
        entry["partial"] = b""
    if stat.st_size == entry["offset"]:
        return []
    try:
        with path.open("rb") as handle:
            handle.seek(entry["offset"])
            data = handle.read()
    except OSError:
        return []
    entry["offset"] += len(data)
    data = entry.get("partial", b"") + data
    lines = data.split(b"\n")
    entry["partial"] = lines.pop()
    return [line.decode("utf-8", "replace") for line in lines if line.strip()]


def _timestamp(value) -> float:
    if isinstance(value, (int, float)):
        return value / 1000 if value > 1e11 else float(value)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0.0
    return 0.0


def _short(text: str, limit: int = SHORT) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def new_summary() -> dict:
    return {
        "session_id": "", "cwd": "", "model": "", "version": "", "git_branch": "",
        "turns": 0, "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
        "cache_create": 0, "context_tokens": 0, "context_window": 0,
        "last_kind": "", "pending_tool": "", "working": False, "last_at": 0.0,
        "started_at": 0.0, "title": "", "first_prompt": "", "effort": "",
        "limits": None, "_message_id": "",
    }


# -- Claude Code ---------------------------------------------------------------


def encode_cwd(cwd: str) -> str:
    """The directory name Claude Code files a working directory's transcripts
    under: every separator and dot becomes a dash."""
    return "".join("-" if char in "/\\:_." else char for char in cwd)


def parse_claude_session_file(text: str) -> dict | None:
    """~/.claude/sessions/<pid>.json: which process is which conversation."""
    try:
        info = json.loads(text)
    except ValueError:
        return None
    if not isinstance(info, dict) or not isinstance(info.get("pid"), int):
        return None
    if not isinstance(info.get("sessionId"), str) or not isinstance(info.get("cwd"), str):
        return None
    return {
        "pid": info["pid"],
        "session_id": info["sessionId"],
        "cwd": info["cwd"],
        "started_at": _timestamp(info.get("startedAt")),
        "status": info.get("status") if isinstance(info.get("status"), str) else "",
        "name": info.get("name") if isinstance(info.get("name"), str) else "",
        "kind": info.get("kind") if isinstance(info.get("kind"), str) else "",
        "version": info.get("version") if isinstance(info.get("version"), str) else "",
    }


def _tool_description(block: dict) -> str:
    """"Bash: pytest -q", from a tool_use block: the name and its one telling argument."""
    name = str(block.get("name") or "tool")
    arguments = block.get("input")
    if not isinstance(arguments, dict):
        return name
    for key in ("command", "file_path", "pattern", "description", "query", "url", "prompt", "skill"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return f"{name}: {_short(value, 60)}"
    return name


def feed_claude_line(summary: dict, line: str) -> None:
    """Fold one line of a Claude Code transcript into ``summary``.

    One API reply is written as several lines, one per content block, each
    repeating the same message id and the same usage. Usage is therefore
    counted once per id, and a turn is one id. Sidechain lines belong to a
    subagent and are counted under the subagents directory instead.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return
    if not isinstance(entry, dict) or entry.get("isSidechain"):
        return
    kind = entry.get("type")
    for key, field_name in (("sessionId", "session_id"), ("cwd", "cwd"),
                            ("version", "version"), ("gitBranch", "git_branch")):
        value = entry.get(key)
        if isinstance(value, str) and value:
            summary[field_name] = value
    if kind == "ai-title" and isinstance(entry.get("aiTitle"), str):
        summary["title"] = entry["aiTitle"]
        return
    if kind == "summary" and isinstance(entry.get("summary"), str) and not summary["title"]:
        summary["title"] = entry["summary"]
        return
    if kind not in ("assistant", "user"):
        return
    stamp = _timestamp(entry.get("timestamp"))
    if stamp:
        summary["last_at"] = stamp
        if not summary["started_at"]:
            summary["started_at"] = stamp
    message = entry.get("message")
    if not isinstance(message, dict):
        return
    content = message.get("content")
    blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
    if kind == "assistant":
        model = message.get("model")
        if isinstance(model, str) and model:
            summary["model"] = model
        effort = entry.get("effort")
        if isinstance(effort, str):
            summary["effort"] = effort
        message_id = str(message.get("id") or "")
        usage = message.get("usage")
        if isinstance(usage, dict) and message_id != summary["_message_id"]:
            summary["_message_id"] = message_id
            summary["turns"] += 1

            def count(key: str) -> int:
                value = usage.get(key)
                return value if isinstance(value, int) and value > 0 else 0

            read, created, given = count("cache_read_input_tokens"), count("cache_creation_input_tokens"), count("input_tokens")
            summary["input_tokens"] += given
            summary["output_tokens"] += count("output_tokens")
            summary["cache_read"] += read
            summary["cache_create"] += created
            # What the model was given this turn is the context: the cached
            # prefix plus the new input. On the first turn nothing is cached
            # yet and the whole prompt is being written to the cache instead.
            summary["context_tokens"] = given + (read if read else created)
        tools = [block for block in blocks if block.get("type") == "tool_use"]
        if tools:
            summary["pending_tool"] = _tool_description(tools[-1])
        elif message.get("stop_reason") == "end_turn":
            summary["pending_tool"] = ""
        summary["last_kind"] = "assistant"
        summary["working"] = message.get("stop_reason") not in ("end_turn", "stop_sequence", "max_tokens")
    else:
        if any(block.get("type") == "tool_result" for block in blocks):
            summary["last_kind"] = "tool_result"
            summary["pending_tool"] = ""
        else:
            summary["last_kind"] = "user"
            summary["pending_tool"] = ""
            if not summary["first_prompt"]:
                text = content if isinstance(content, str) else " ".join(
                    str(block.get("text") or "") for block in blocks if block.get("type") == "text"
                )
                summary["first_prompt"] = _short(text)
        summary["working"] = True


def parse_claude_limits(text: str) -> dict | None:
    """abtop's rate-limit file, in the shape Codex's limits take.

    Both windows become "primary" and "secondary" with their length in
    minutes, so one renderer draws either agent's. The file's own write
    time comes along: it is only rewritten while a Claude Code session is
    drawing its status line, so its age is part of the answer.
    """
    try:
        info = json.loads(text)
    except ValueError:
        return None
    if not isinstance(info, dict):
        return None
    parsed: dict = {}
    for key, name, minutes in CLAUDE_LIMIT_WINDOWS:
        window = info.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percentage")
        resets = window.get("resets_at")
        parsed[name] = {
            "used_percent": float(used) if isinstance(used, (int, float)) else None,
            "window_minutes": minutes,
            "resets_at": resets if isinstance(resets, (int, float)) else None,
        }
    if not parsed:
        return None
    updated = info.get("updated_at")
    if isinstance(updated, (int, float)):
        parsed["updated_at"] = float(updated)
    return parsed


def read_claude_limits(home: Path) -> dict | None:
    return parse_claude_limits(_read(home / CLAUDE_LIMITS_FILE))


def claude_context_window(model: str) -> int:
    return CONTEXT_WINDOW_1M if "[1m]" in model.lower() else CONTEXT_WINDOW


def claude_status(summary: dict, *, alive: bool, hint: str = "") -> str:
    """What the conversation is doing, from its last few lines.

    A tool_use with no result yet means a tool is running. A prompt or a
    result with no reply yet means the model is generating. The session
    file's own "busy" covers a reply still streaming, which the transcript
    does not show until it lands.
    """
    if not alive:
        return "finished"
    if summary["pending_tool"]:
        return "running " + summary["pending_tool"].split(":")[0]
    if summary["last_kind"] in ("user", "tool_result"):
        return "thinking"
    if hint == "busy":
        return "working"
    return "waiting for input"


def _newest(paths: list[Path], *, after: float = 0.0) -> Path | None:
    best: tuple[float, Path] | None = None
    for path in paths:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if mtime >= after and (best is None or mtime > best[0]):
            best = (mtime, path)
    return best[1] if best else None


def _subagents(project_dir: Path, session_id: str, now: float) -> tuple[int, int]:
    """How many subagent transcripts the session has, and how many are live."""
    directory = project_dir / session_id / "subagents"
    try:
        transcripts = list(directory.glob("agent-*.jsonl"))
    except OSError:
        return 0, 0
    active = 0
    for transcript in transcripts:
        try:
            if now - transcript.stat().st_mtime <= SUBAGENT_ACTIVE_SECONDS:
                active += 1
        except OSError:
            continue
    return len(transcripts), active


def _summarise(path: Path, cache: dict, feed) -> dict:
    entry = cache.setdefault(str(path), {})
    # tail_lines owns and may clear its own dict, so the summary lives
    # beside it rather than in it.
    summary = entry.setdefault("summary", new_summary())
    for line in tail_lines(path, entry.setdefault("tail", {})):
        feed(summary, line)
    return summary


def read_claude_sessions(
    home: Path,
    *,
    processes: list[AgentProcess],
    cache: dict,
    now: float | None = None,
) -> list[AgentSession]:
    """One row per running Claude Code process that has a transcript.

    The sessions directory maps a pid to a conversation; a process without
    an entry there (an older Claude Code, or one started another way) is
    matched to the newest transcript under its working directory's project
    instead. /clear starts a new transcript without touching the sessions
    file, so when a directory has one Claude process the newest transcript
    written since it started wins over the one the file names.
    """
    now = time.time() if now is None else now
    base = home / ".claude"
    projects = base / "projects"
    claude = {process.pid: process for process in processes if process.agent == "claude"}
    by_cwd: dict[str, int] = {}
    for process in claude.values():
        by_cwd[process.cwd] = by_cwd.get(process.cwd, 0) + 1
    files: dict[int, dict] = {}
    try:
        session_files = list((base / "sessions").glob("*.json"))
    except OSError:
        session_files = []
    for path in session_files:
        info = parse_claude_session_file(_read(path))
        if info and info["pid"] in claude:
            files[info["pid"]] = info
    limits = read_claude_limits(home)
    sessions = []
    claimed: set[Path] = set()
    for pid, process in sorted(claude.items()):
        info = files.get(pid)
        cwd = info["cwd"] if info else process.cwd
        project_dir = projects / encode_cwd(cwd)
        transcript = None
        if info:
            named = project_dir / f"{info['session_id']}.jsonl"
            transcript = named if named.exists() else None
            if by_cwd.get(process.cwd, 0) <= 1:
                newest = _newest(list(project_dir.glob("*.jsonl")), after=info["started_at"] - 5)
                if newest is not None and (transcript is None or newest != transcript):
                    transcript = newest
        if transcript is None:
            transcript = _newest(
                [path for path in project_dir.glob("*.jsonl") if path not in claimed],
                after=process.started_at - 5,
            )
        # Two processes on one transcript is a forked helper, not two
        # conversations; the first pid keeps it.
        if transcript is None or transcript in claimed:
            continue
        claimed.add(transcript)
        summary = _summarise(transcript, cache, feed_claude_line)
        model = summary["model"]
        session_id = summary["session_id"] or transcript.stem
        count, active = _subagents(project_dir, session_id, now)
        sessions.append(AgentSession(
            agent="claude",
            pid=pid,
            project=Path(cwd).name or cwd,
            cwd=cwd,
            title=summary["title"] or summary["first_prompt"] or (info["name"] if info else ""),
            model=model,
            status=claude_status(summary, alive=True, hint=info["status"] if info else ""),
            task=summary["pending_tool"],
            context_tokens=summary["context_tokens"],
            context_window=claude_context_window(model),
            input_tokens=summary["input_tokens"],
            output_tokens=summary["output_tokens"],
            cache_read=summary["cache_read"],
            cache_create=summary["cache_create"],
            turns=summary["turns"],
            started_at=(info["started_at"] if info and info["started_at"] else summary["started_at"]) or process.started_at,
            last_at=summary["last_at"],
            git_branch=summary["git_branch"],
            version=summary["version"] or (info["version"] if info else ""),
            limits=limits,
            subagents=count,
            subagents_active=active,
        ))
    return sessions


# -- Codex CLI -----------------------------------------------------------------


def _usage_int(usage: dict, key: str) -> int:
    value = usage.get(key)
    return value if isinstance(value, int) and value > 0 else 0


def feed_codex_line(summary: dict, line: str) -> None:
    """Fold one line of a Codex rollout into ``summary``.

    token_count carries running totals rather than deltas, so it replaces
    rather than adds. The rate limits ride on the same event.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return
    if not isinstance(entry, dict):
        return
    kind = entry.get("type")
    payload = entry.get("payload")
    payload = payload if isinstance(payload, dict) else {}
    stamp = _timestamp(entry.get("timestamp"))
    if stamp:
        summary["last_at"] = stamp
    if kind == "session_meta":
        for key, field_name in (("cwd", "cwd"), ("cli_version", "version"), ("id", "session_id")):
            value = payload.get(key)
            if isinstance(value, str):
                summary[field_name] = value
        started = _timestamp(payload.get("timestamp"))
        if started:
            summary["started_at"] = started
        return
    if kind == "turn_context":
        for key, field_name in (("model", "model"), ("effort", "effort"), ("cwd", "cwd")):
            value = payload.get(key)
            if isinstance(value, str) and value:
                summary[field_name] = value
        return
    if kind == "event_msg":
        event = payload.get("type")
        if event == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                total = info.get("total_token_usage")
                if isinstance(total, dict):
                    summary["input_tokens"] = _usage_int(total, "input_tokens")
                    summary["output_tokens"] = _usage_int(total, "output_tokens")
                    summary["cache_read"] = _usage_int(total, "cached_input_tokens")
                    summary["cache_create"] = _usage_int(total, "cache_write_input_tokens")
                last = info.get("last_token_usage")
                if isinstance(last, dict):
                    summary["context_tokens"] = _usage_int(last, "total_tokens") or (
                        _usage_int(last, "input_tokens") + _usage_int(last, "output_tokens")
                    )
                window = info.get("model_context_window")
                if isinstance(window, int) and window > 0:
                    summary["context_window"] = window
            limits = payload.get("rate_limits")
            if isinstance(limits, dict):
                summary["limits"] = parse_codex_limits(limits)
        elif event == "task_started":
            summary["working"] = True
            summary["pending_tool"] = ""
            window = payload.get("model_context_window")
            if isinstance(window, int) and window > 0:
                summary["context_window"] = window
        elif event == "task_complete":
            summary["working"] = False
            summary["pending_tool"] = ""
            summary["turns"] += 1
        elif event == "thread_settings_applied":
            settings = payload.get("thread_settings")
            if isinstance(settings, dict) and isinstance(settings.get("model"), str):
                summary["model"] = settings["model"]
        elif event == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "UserMessage" and not summary["first_prompt"]:
                content = item.get("content")
                if isinstance(content, list):
                    summary["first_prompt"] = _short(" ".join(
                        str(block.get("text") or "") for block in content if isinstance(block, dict)
                    ))
        return
    if kind == "response_item":
        item = payload.get("type")
        if item in ("custom_tool_call", "function_call"):
            name = str(payload.get("name") or "tool")
            argument = payload.get("arguments") if item == "function_call" else payload.get("input")
            summary["pending_tool"] = f"{name}: {_short(str(argument), 60)}" if isinstance(argument, str) and argument else name
            summary["working"] = True
        elif item in ("custom_tool_call_output", "function_call_output"):
            summary["pending_tool"] = ""


def parse_codex_limits(limits: dict) -> dict | None:
    """Codex's two rate-limit windows as {"primary": {...}, "secondary": {...}}."""
    parsed = {}
    for key in ("primary", "secondary"):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percent")
        minutes = window.get("window_minutes")
        resets = window.get("resets_at")
        parsed[key] = {
            "used_percent": float(used) if isinstance(used, (int, float)) else None,
            "window_minutes": minutes if isinstance(minutes, int) else None,
            "resets_at": resets if isinstance(resets, (int, float)) else None,
        }
    plan = limits.get("plan_type")
    if isinstance(plan, str):
        parsed["plan"] = plan
    return parsed or None


def codex_status(summary: dict, *, alive: bool) -> str:
    if not alive:
        return "finished"
    if summary["pending_tool"]:
        return "running " + summary["pending_tool"].split(":")[0]
    if summary["working"]:
        return "thinking"
    return "waiting for input"


def _open_rollouts(root: Path, pid: int, sessions_dir: Path) -> list[Path]:
    """Rollout files a process holds open, from its descriptor table."""
    found = []
    try:
        descriptors = list((root / "proc" / str(pid) / "fd").iterdir())
    except OSError:
        return []
    for descriptor in descriptors:
        try:
            target = Path(os.readlink(descriptor))
        except OSError:
            continue
        if target.name.startswith("rollout-") and target.suffix == ".jsonl" and str(target).startswith(str(sessions_dir)):
            found.append(target)
    return found


def _recent_rollouts(sessions_dir: Path, *, after: float) -> list[Path]:
    """Rollouts written since ``after``, from the newest day directories only."""
    days = []
    for year in sorted(sessions_dir.glob("[0-9]*"), reverse=True)[:1]:
        for month in sorted(year.glob("[0-9]*"), reverse=True)[:1]:
            days.extend(sorted(month.glob("[0-9]*"), reverse=True)[:2])
    found = []
    for day in days:
        for path in day.glob("rollout-*.jsonl"):
            try:
                if path.stat().st_mtime >= after:
                    found.append(path)
            except OSError:
                continue
    return found


def read_codex_sessions(
    home: Path,
    *,
    processes: list[AgentProcess],
    cache: dict,
    root: Path = Path("/"),
    now: float | None = None,
) -> list[AgentSession]:
    now = time.time() if now is None else now
    sessions_dir = home / ".codex" / "sessions"
    found = []
    claimed: set[Path] = set()
    for process in sorted(processes, key=lambda p: p.pid):
        if process.agent != "codex":
            continue
        rollouts = [path for path in _open_rollouts(root, process.pid, sessions_dir) if path not in claimed]
        if not rollouts:
            candidates = [
                path for path in _recent_rollouts(sessions_dir, after=process.started_at - 5)
                if path not in claimed
            ]
            matching = []
            for path in candidates:
                summary = _summarise(path, cache, feed_codex_line)
                if summary["cwd"] == process.cwd:
                    matching.append(path)
            newest = _newest(matching)
            rollouts = [newest] if newest else []
        for rollout in rollouts:
            claimed.add(rollout)
            summary = _summarise(rollout, cache, feed_codex_line)
            cwd = summary["cwd"] or process.cwd
            found.append(AgentSession(
                agent="codex",
                pid=process.pid,
                project=Path(cwd).name or cwd,
                cwd=cwd,
                title=summary["first_prompt"],
                model=summary["model"],
                status=codex_status(summary, alive=True),
                task=summary["pending_tool"],
                context_tokens=summary["context_tokens"],
                context_window=summary["context_window"],
                input_tokens=summary["input_tokens"],
                output_tokens=summary["output_tokens"],
                cache_read=summary["cache_read"],
                cache_create=summary["cache_create"],
                turns=summary["turns"],
                started_at=summary["started_at"] or process.started_at,
                last_at=summary["last_at"],
                version=summary["version"],
                limits=summary["limits"],
            ))
    return found


# -- OpenCode ------------------------------------------------------------------


OPENCODE_QUERY = """
SELECT id, title, directory, model, tokens_input, tokens_output,
       tokens_cache_read, tokens_cache_write, time_created, time_updated
FROM session
WHERE parent_id IS NULL
ORDER BY time_updated DESC
LIMIT 50
"""


def opencode_database(home: Path) -> Path:
    return home / ".local" / "share" / "opencode" / "opencode.db"


def query_opencode(database: Path) -> list[tuple]:
    """The newest top-level sessions, or [] if the database is not readable.

    Opened read-only through a URI so a database OpenCode is writing to is
    never locked by this, and the whole thing is one query so a schema this
    does not recognise costs an empty card rather than a traceback.
    """
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True, timeout=0.5)
        try:
            return connection.execute(OPENCODE_QUERY).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return []


def read_opencode_sessions(
    home: Path,
    *,
    processes: list[AgentProcess],
    now: float | None = None,
    rows: list[tuple] | None = None,
) -> list[AgentSession]:
    """OpenCode's sessions, matched to its processes by working directory.

    The database records every conversation ever had, so a row is shown
    only when a running OpenCode has its directory, or when it was written
    to in the last couple of minutes. Status comes from the update time:
    the database has no notion of a turn in progress.
    """
    now = time.time() if now is None else now
    database = opencode_database(home)
    if rows is None:
        if not database.exists():
            return []
        rows = query_opencode(database)
    waiting = {process.cwd: process for process in processes if process.agent == "opencode"}
    found = []
    for row in rows:
        if len(row) < 10:
            continue
        session_id, title, directory, model, tokens_in, tokens_out, cache_read, cache_write, created, updated = row[:10]
        directory = str(directory or "")
        updated_at = _timestamp(updated or 0)
        process = waiting.pop(directory, None)
        if process is None and now - updated_at > RECENT_SECONDS:
            continue
        found.append(AgentSession(
            agent="opencode",
            pid=process.pid if process else 0,
            project=Path(directory).name or directory,
            cwd=directory,
            title=str(title or ""),
            model=str(model or ""),
            status=("working" if now - updated_at <= 10 else "waiting for input") if process else "finished",
            input_tokens=int(tokens_in or 0),
            output_tokens=int(tokens_out or 0),
            cache_read=int(cache_read or 0),
            cache_create=int(cache_write or 0),
            started_at=_timestamp(created or 0),
            last_at=updated_at,
        ))
    return found


# -- everything together -------------------------------------------------------


def installed_agents(home: Path, dpkg_installed: set[str]) -> list[str]:
    """The catalog's AI tools that are installed, by name, as Software sees them."""
    try:
        import catalog
    except ImportError:
        return []
    names = []
    for entry in catalog.ENTRIES:
        if entry.category == CATALOG_CATEGORY and catalog.installed(entry, dpkg_installed, home):
            names.append(entry.name)
    return names


def collect(
    *,
    root: Path = Path("/"),
    home: Path | None = None,
    cache: dict,
    now: float | None = None,
    dpkg_installed: set[str] | None = None,
) -> dict:
    """One snapshot of the agents: processes, the sessions behind them, and
    what is installed. ``cache`` persists between calls and holds the
    transcript offsets."""
    home = home or Path.home()
    now = time.time() if now is None else now
    processes = read_processes(root, now=now)
    sessions = (
        read_claude_sessions(home, processes=processes, cache=cache, now=now)
        + read_codex_sessions(home, processes=processes, cache=cache, root=root, now=now)
        + read_opencode_sessions(home, processes=processes, now=now)
    )
    with_session = {session.pid for session in sessions if session.pid}
    return {
        "processes": [asdict(process) for process in processes],
        "sessions": [{**asdict(session), "context_percent": session.context_percent} for session in sessions],
        "unmatched": [asdict(process) for process in processes if process.pid not in with_session],
        "installed": installed_agents(home, dpkg_installed) if dpkg_installed is not None else [],
        "names": dict(AGENTS),
    }
