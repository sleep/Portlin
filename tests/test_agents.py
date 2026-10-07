"""The agent readers, against lines captured from the agents' own files.

The transcript formats here are somebody else's: Claude Code's one line per
content block, Codex's one line per event, OpenCode's SQLite rows. None of
them is documented, so each sample is a real line, cut down, and the tests
assert what the HUD makes of it rather than what the format promises.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from conftest import load_tool


@pytest.fixture(scope="module")
def agents():
    return load_tool("agents.py")


USAGE = {"input_tokens": 2, "cache_creation_input_tokens": 16650, "cache_read_input_tokens": 25450,
         "output_tokens": 219}


def assistant(message_id: str, blocks: list, *, usage=None, stop="tool_use", stamp="2026-10-07T06:32:26.537Z") -> str:
    return json.dumps({
        "type": "assistant", "isSidechain": False, "timestamp": stamp, "cwd": "/home/me/portlin",
        "sessionId": "348257f3", "version": "2.1.292", "gitBranch": "main", "effort": "high",
        "message": {"model": "claude-fable-5-1", "id": message_id, "role": "assistant",
                    "content": blocks, "stop_reason": stop, "usage": usage or USAGE},
    })


def user(content, *, stamp="2026-10-07T06:32:23.380Z") -> str:
    return json.dumps({
        "type": "user", "isSidechain": False, "timestamp": stamp, "cwd": "/home/me/portlin",
        "sessionId": "348257f3", "message": {"role": "user", "content": content},
    })


TOOL_USE = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "pytest -q tests/"}}
TOOL_RESULT = [{"tool_use_id": "toolu_1", "type": "tool_result", "content": "67 passed"}]


class TestProcessMatching:
    @pytest.mark.parametrize("argv, agent", [
        (["claude"], "claude"),
        (["/usr/local/bin/claude", "--resume"], "claude"),
        (["node", "/home/me/.npm/lib/node_modules/@anthropic-ai/claude-code/cli.js"], None),
        (["/home/me/.local/share/claude/versions/2.1.292", "--foo"], "claude"),
        (["/home/me/.local/bin/codex"], "codex"),
        (["node", "/home/me/.codex/packages/codex/bin/codex.js"], "codex"),
        (["/home/me/.opencode/bin/opencode"], "opencode"),
        (["python3", "/home/me/.local/bin/hermes"], "hermes"),
        (["/usr/bin/claude-launch"], None),
        (["bash", "-c", "claude"], None),
        ([""], None),
    ])
    def test_the_binary_names_the_agent(self, agents, argv, agent):
        assert agents.match_agent(argv) == agent

    def test_proc_stat_survives_a_comm_with_spaces_and_parens(self, agents):
        line = ("166 (my (odd) name) R 1 1 1 0 -1 4194304 95 0 0 0 7 3 0 0 20 0 1 0 6981872 2646016 260 "
                "18446744073709551615 0 0 0 0 0 0 0 0 0 0 0 0 17 7 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        stat = agents.parse_proc_stat_line(line)
        assert stat["comm"] == "my (odd) name"
        assert (stat["ppid"], stat["utime"], stat["stime"], stat["starttime"]) == (1, 7, 3, 6981872)
        assert agents.parse_proc_stat_line("garbage") is None

    def test_the_process_reader_finds_agents_and_their_children(self, agents, tmp_path):
        def process(pid: int, ppid: int, comm: str, argv: list[str], starttime: int = 100) -> None:
            directory = tmp_path / "proc" / str(pid)
            directory.mkdir(parents=True)
            fields = ["R", str(ppid)] + ["0"] * 9 + ["500", "250"] + ["0"] * 6 + [str(starttime)] + ["0"] * 5
            (directory / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields) + "\n")
            (directory / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
            (directory / "statm").write_text("1000 2500 100 0 0 0 0\n")
            os.symlink("/home/me/portlin", directory / "cwd")

        (tmp_path / "proc").mkdir()
        (tmp_path / "proc/uptime").write_text("1000.00 4000.00\n")
        process(10, 1, "bash", ["bash"])
        process(20, 10, "claude", ["claude"], starttime=50_000)
        process(21, 20, "pytest", ["python3", "-m", "pytest"])
        process(22, 20, "node", ["node", "helper.js"])
        process(30, 1, "codex", ["/home/me/.local/bin/codex"])
        process(99, 1, "hud", ["python3", "portlin-hud"])

        found = agents.read_processes(tmp_path, self_pid=99, clock_ticks=100, page_size=4096, now=2000.0)
        assert [(p.pid, p.agent) for p in found] == [(20, "claude"), (30, "codex")]
        claude = found[0]
        assert claude.children == ("node", "pytest")
        assert claude.rss_bytes == 2500 * 4096
        assert claude.cpu_seconds == 7.5
        assert claude.cwd == "/home/me/portlin"
        # Boot was at 1000 and the process started 500 s after it.
        assert claude.started_at == 1500.0


class TestTailing:
    def test_only_new_complete_lines_come_back(self, agents, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_bytes(b'{"a":1}\n{"b":2}\n{"c":')
        entry: dict = {}
        assert agents.tail_lines(path, entry) == ['{"a":1}', '{"b":2}']
        assert agents.tail_lines(path, entry) == []
        with path.open("ab") as handle:
            handle.write(b'3}\n')
        assert agents.tail_lines(path, entry) == ['{"c":3}']

    def test_a_replaced_file_starts_over(self, agents, tmp_path):
        path = tmp_path / "t.jsonl"
        path.write_bytes(b'{"a":1}\n{"b":2}\n')
        entry: dict = {}
        agents.tail_lines(path, entry)
        path.unlink()
        path.write_bytes(b'{"z":9}\n')
        assert agents.tail_lines(path, entry) == ['{"z":9}']

    def test_a_missing_file_is_nothing_new(self, agents, tmp_path):
        assert agents.tail_lines(tmp_path / "missing", {}) == []


class TestClaudeTranscripts:
    def test_cwd_encoding(self, agents):
        assert agents.encode_cwd("/home/me/vibe/portlin") == "-home-me-vibe-portlin"
        assert agents.encode_cwd("/home/me/a.b_c") == "-home-me-a-b-c"

    def test_one_reply_split_across_lines_is_one_turn_counted_once(self, agents):
        summary = agents.new_summary()
        for line in (
            user("add a feature"),
            assistant("msg_1", [{"type": "thinking", "thinking": ""}]),
            assistant("msg_1", [{"type": "text", "text": "Looking."}]),
            assistant("msg_1", [TOOL_USE]),
        ):
            agents.feed_claude_line(summary, line)
        assert summary["turns"] == 1
        assert summary["input_tokens"] == 2 and summary["output_tokens"] == 219
        assert summary["cache_read"] == 25450 and summary["cache_create"] == 16650
        assert summary["context_tokens"] == 2 + 25450
        assert summary["model"] == "claude-fable-5-1"
        assert summary["git_branch"] == "main" and summary["version"] == "2.1.292"
        assert summary["first_prompt"] == "add a feature"
        assert summary["pending_tool"] == "Bash: pytest -q tests/"
        assert summary["started_at"] < summary["last_at"]

    def test_the_first_turn_counts_the_cache_it_is_writing(self, agents):
        summary = agents.new_summary()
        usage = {"input_tokens": 4, "cache_creation_input_tokens": 9000, "cache_read_input_tokens": 0, "output_tokens": 1}
        agents.feed_claude_line(summary, assistant("msg_1", [{"type": "text", "text": "hi"}], usage=usage, stop="end_turn"))
        assert summary["context_tokens"] == 9004

    def test_status_follows_the_last_line(self, agents):
        summary = agents.new_summary()
        agents.feed_claude_line(summary, user("go"))
        assert agents.claude_status(summary, alive=True) == "thinking"
        agents.feed_claude_line(summary, assistant("msg_1", [TOOL_USE]))
        assert agents.claude_status(summary, alive=True) == "running Bash"
        agents.feed_claude_line(summary, user(TOOL_RESULT))
        assert agents.claude_status(summary, alive=True) == "thinking"
        agents.feed_claude_line(summary, assistant("msg_2", [{"type": "text", "text": "Done."}], stop="end_turn"))
        assert agents.claude_status(summary, alive=True) == "waiting for input"
        assert agents.claude_status(summary, alive=True, hint="busy") == "working"
        assert agents.claude_status(summary, alive=False) == "finished"

    def test_titles_sidechains_and_junk(self, agents):
        summary = agents.new_summary()
        agents.feed_claude_line(summary, '{"type":"ai-title","aiTitle":"Portlin HUD","sessionId":"x"}')
        agents.feed_claude_line(summary, 'not json at all')
        agents.feed_claude_line(summary, '[1, 2, 3]')
        sidechain = json.loads(assistant("msg_9", [TOOL_USE]))
        sidechain["isSidechain"] = True
        agents.feed_claude_line(summary, json.dumps(sidechain))
        assert summary["title"] == "Portlin HUD"
        assert summary["turns"] == 0 and summary["pending_tool"] == ""

    def test_rate_limits_come_from_abtops_file(self, agents, tmp_path):
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude/abtop-rate-limits.json").write_text(json.dumps({
            "source": "claude", "updated_at": 1791357412,
            "five_hour": {"used_percentage": 1, "resets_at": 1791374400},
            "seven_day": {"used_percentage": 77, "resets_at": 1791518400}}))
        limits = agents.read_claude_limits(tmp_path)
        assert limits["primary"] == {"used_percent": 1.0, "window_minutes": 300, "resets_at": 1791374400}
        assert limits["secondary"]["used_percent"] == 77.0 and limits["secondary"]["window_minutes"] == 10080
        assert limits["updated_at"] == 1791357412.0

    def test_no_file_or_a_strange_one_is_no_limits(self, agents, tmp_path):
        assert agents.read_claude_limits(tmp_path) is None
        assert agents.parse_claude_limits("{}") is None
        assert agents.parse_claude_limits("[1]") is None
        assert agents.parse_claude_limits("not json") is None

    def test_the_context_window_follows_the_model_name(self, agents):
        assert agents.claude_context_window("claude-fable-5-1") == 200_000
        assert agents.claude_context_window("claude-sonnet-5-5[1m]") == 1_000_000

    def test_session_file(self, agents):
        info = agents.parse_claude_session_file(json.dumps({
            "pid": 84894, "sessionId": "348257f3", "cwd": "/home/me/portlin",
            "startedAt": 1791354603926, "status": "busy", "name": "portlin-58", "version": "2.1.292"}))
        assert info["pid"] == 84894 and info["status"] == "busy"
        assert info["started_at"] == 1791354603.926
        assert agents.parse_claude_session_file('{"pid": "x"}') is None

    def _process(self, agents, pid: int, cwd: str, started: float = 1000.0):
        return agents.AgentProcess(pid=pid, ppid=1, agent="claude", command="claude", cwd=cwd,
                                   rss_bytes=0, cpu_seconds=0.0, started_at=started)

    def test_sessions_are_found_through_the_session_file(self, agents, tmp_path):
        home = tmp_path
        project = home / ".claude/projects/-home-me-portlin"
        project.mkdir(parents=True)
        (project / "348257f3.jsonl").write_text(user("add a feature") + "\n" + assistant("msg_1", [TOOL_USE]) + "\n")
        (project / "348257f3/subagents").mkdir(parents=True)
        (project / "348257f3/subagents/agent-abc.jsonl").write_text("{}\n")
        (home / ".claude/sessions").mkdir()
        (home / ".claude/sessions/20.json").write_text(json.dumps({
            "pid": 20, "sessionId": "348257f3", "cwd": "/home/me/portlin", "startedAt": 1791354603926,
            "status": "busy", "name": "portlin-58"}))
        (home / ".claude/sessions/77.json").write_text(json.dumps({
            "pid": 77, "sessionId": "dead", "cwd": "/home/me/portlin", "startedAt": 1}))

        cache: dict = {}
        now = 1791354700.0
        sessions = agents.read_claude_sessions(home, processes=[self._process(agents, 20, "/home/me/portlin")],
                                               cache=cache, now=now)
        assert len(sessions) == 1
        session = sessions[0]
        assert (session.agent, session.pid, session.project) == ("claude", 20, "portlin")
        assert session.status == "running Bash" and session.task == "Bash: pytest -q tests/"
        assert session.title == "add a feature"
        assert session.context_window == 200_000 and session.context_tokens == 25452
        assert session.subagents == 1 and session.subagents_active == 1
        assert session.limits is None
        assert session.started_at == 1791354603.926
        assert round(session.context_percent, 2) == 12.73

        # A second read parses nothing again and still answers the same.
        again = agents.read_claude_sessions(home, processes=[self._process(agents, 20, "/home/me/portlin")],
                                            cache=cache, now=now)
        assert again[0].turns == 1 and cache[str(project / "348257f3.jsonl")]["tail"]["offset"] > 0

    def test_a_process_without_a_session_file_takes_the_newest_transcript(self, agents, tmp_path):
        home = tmp_path
        project = home / ".claude/projects/-home-me-portlin"
        project.mkdir(parents=True)
        old = project / "old.jsonl"
        old.write_text(user("old") + "\n")
        os.utime(old, (900, 900))
        new = project / "new.jsonl"
        new.write_text(user("new") + "\n")
        os.utime(new, (1500, 1500))
        sessions = agents.read_claude_sessions(home, processes=[self._process(agents, 20, "/home/me/portlin", 1000.0)],
                                               cache={}, now=1600.0)
        assert [session.title for session in sessions] == ["new"]

    def test_two_processes_never_share_a_transcript(self, agents, tmp_path):
        home = tmp_path
        project = home / ".claude/projects/-home-me-portlin"
        project.mkdir(parents=True)
        (project / "only.jsonl").write_text(user("one") + "\n")
        processes = [self._process(agents, 20, "/home/me/portlin"), self._process(agents, 21, "/home/me/portlin")]
        assert len(agents.read_claude_sessions(home, processes=processes, cache={}, now=2000.0)) == 1

    def test_no_claude_directory_is_no_sessions(self, agents, tmp_path):
        assert agents.read_claude_sessions(tmp_path, processes=[self._process(agents, 1, "/x")], cache={}) == []


CODEX_META = json.dumps({"timestamp": "2026-09-14T23:04:42.482Z", "type": "session_meta", "payload": {
    "id": "01a0a21d", "timestamp": "2026-09-14T22:50:36.792Z", "cwd": "/home/me/adbot",
    "originator": "codex-tui", "cli_version": "0.154.0", "source": "cli", "model_provider": "openai"}})
CODEX_STARTED = json.dumps({"timestamp": "2026-09-14T23:04:42.482Z", "type": "event_msg", "payload": {
    "type": "task_started", "turn_id": "t1", "model_context_window": 258400}})
CODEX_TURN = json.dumps({"timestamp": "2026-09-14T23:04:42.878Z", "type": "turn_context", "payload": {
    "turn_id": "t1", "cwd": "/home/me/adbot", "model": "gpt-6-astra", "effort": "medium"}})
CODEX_PROMPT = json.dumps({"timestamp": "2026-09-14T23:04:42.892Z", "type": "event_msg", "payload": {
    "type": "item_completed", "item": {"type": "UserMessage", "content": [
        {"type": "text", "text": "Can you improve the layout of the /api-docs page?"}]}}})
CODEX_CALL = json.dumps({"timestamp": "2026-09-14T23:04:50.733Z", "type": "response_item", "payload": {
    "type": "custom_tool_call", "name": "exec", "input": "text(await tools.exec_command({cmd:\"cat docs/README.md\"}))"}})
CODEX_OUTPUT = json.dumps({"timestamp": "2026-09-14T23:04:53.397Z", "type": "response_item", "payload": {
    "type": "custom_tool_call_output", "output": []}})
CODEX_TOKENS = json.dumps({"timestamp": "2026-09-14T23:04:53.397Z", "type": "event_msg", "payload": {
    "type": "token_count", "info": {
        "total_token_usage": {"input_tokens": 18076, "cached_input_tokens": 11904, "cache_write_input_tokens": 0,
                              "output_tokens": 193, "reasoning_output_tokens": 0, "total_tokens": 18269},
        "last_token_usage": {"input_tokens": 18076, "cached_input_tokens": 11904, "cache_write_input_tokens": 0,
                             "output_tokens": 193, "reasoning_output_tokens": 0, "total_tokens": 18269},
        "model_context_window": 258400},
    "rate_limits": {"primary": {"used_percent": 29.0, "window_minutes": 300, "resets_at": 1789435789},
                    "secondary": {"used_percent": 11.0, "window_minutes": 10080, "resets_at": 1790003491},
                    "plan_type": "plus"}}})
CODEX_DONE = json.dumps({"timestamp": "2026-09-14T23:08:36.525Z", "type": "event_msg", "payload": {
    "type": "task_complete", "turn_id": "t1", "last_agent_message": "Refresh /api-docs."}})


class TestCodexRollouts:
    def test_a_turn_from_start_to_finish(self, agents):
        summary = agents.new_summary()
        for line in (CODEX_META, CODEX_STARTED, CODEX_TURN, CODEX_PROMPT):
            agents.feed_codex_line(summary, line)
        assert summary["cwd"] == "/home/me/adbot" and summary["version"] == "0.154.0"
        assert summary["model"] == "gpt-6-astra" and summary["effort"] == "medium"
        assert summary["context_window"] == 258400
        assert summary["first_prompt"].startswith("Can you improve")
        assert agents.codex_status(summary, alive=True) == "thinking"
        agents.feed_codex_line(summary, CODEX_CALL)
        assert agents.codex_status(summary, alive=True) == "running exec"
        agents.feed_codex_line(summary, CODEX_OUTPUT)
        agents.feed_codex_line(summary, CODEX_TOKENS)
        assert summary["input_tokens"] == 18076 and summary["cache_read"] == 11904
        assert summary["context_tokens"] == 18269
        assert summary["limits"]["primary"]["used_percent"] == 29.0
        assert summary["limits"]["secondary"]["window_minutes"] == 10080
        assert summary["limits"]["plan"] == "plus"
        agents.feed_codex_line(summary, CODEX_DONE)
        assert summary["turns"] == 1
        assert agents.codex_status(summary, alive=True) == "waiting for input"
        assert summary["started_at"] == 1789426236.792

    def test_totals_replace_rather_than_add(self, agents):
        summary = agents.new_summary()
        agents.feed_codex_line(summary, CODEX_TOKENS)
        agents.feed_codex_line(summary, CODEX_TOKENS)
        assert summary["input_tokens"] == 18076

    def test_rollouts_are_found_through_the_descriptor_table(self, agents, tmp_path):
        home = tmp_path / "home"
        day = home / ".codex/sessions/2026/09/14"
        day.mkdir(parents=True)
        rollout = day / "rollout-2026-09-14T23-50-36-01a0a21d.jsonl"
        rollout.write_text("\n".join([CODEX_META, CODEX_STARTED, CODEX_TURN, CODEX_PROMPT, CODEX_CALL]) + "\n")
        root = tmp_path / "root"
        fd = root / "proc/30/fd"
        fd.mkdir(parents=True)
        os.symlink(str(rollout), fd / "7")
        os.symlink("/dev/null", fd / "0")
        process = agents.AgentProcess(pid=30, ppid=1, agent="codex", command="codex", cwd="/home/me/adbot",
                                      rss_bytes=0, cpu_seconds=0.0, started_at=1.0)
        sessions = agents.read_codex_sessions(home, processes=[process], cache={}, root=root, now=100.0)
        assert len(sessions) == 1
        session = sessions[0]
        assert (session.agent, session.project, session.model) == ("codex", "adbot", "gpt-6-astra")
        assert session.status == "running exec" and session.context_window == 258400

    def test_without_descriptors_the_rollout_is_matched_by_directory(self, agents, tmp_path):
        home = tmp_path / "home"
        day = home / ".codex/sessions/2026/09/14"
        day.mkdir(parents=True)
        other = day / "rollout-a.jsonl"
        other.write_text(CODEX_META.replace("/home/me/adbot", "/home/me/elsewhere") + "\n")
        mine = day / "rollout-b.jsonl"
        mine.write_text(CODEX_META + "\n" + CODEX_PROMPT + "\n")
        process = agents.AgentProcess(pid=30, ppid=1, agent="codex", command="codex", cwd="/home/me/adbot",
                                      rss_bytes=0, cpu_seconds=0.0, started_at=0.0)
        sessions = agents.read_codex_sessions(home, processes=[process], cache={}, root=tmp_path / "root", now=100.0)
        assert [session.title for session in sessions] == ["Can you improve the layout of the /api-docs page?"]


class TestOpenCode:
    def _database(self, path: Path, rows: list[tuple]) -> None:
        connection = sqlite3.connect(path)
        connection.execute("""CREATE TABLE session (
            id text PRIMARY KEY, project_id text NOT NULL, parent_id text, directory text NOT NULL,
            title text NOT NULL, model text, tokens_input integer DEFAULT 0, tokens_output integer DEFAULT 0,
            tokens_cache_read integer DEFAULT 0, tokens_cache_write integer DEFAULT 0,
            time_created integer NOT NULL, time_updated integer NOT NULL)""")
        connection.executemany(
            "INSERT INTO session (id, project_id, parent_id, directory, title, model, tokens_input, tokens_output,"
            " tokens_cache_read, tokens_cache_write, time_created, time_updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            rows)
        connection.commit()
        connection.close()

    def test_sessions_are_matched_to_processes_by_directory(self, agents, tmp_path):
        home = tmp_path
        database = agents.opencode_database(home)
        database.parent.mkdir(parents=True)
        now_ms = 1_800_000_000_000
        self._database(database, [
            ("s1", "p", None, "/home/me/portlin", "HUD work", "anthropic/claude", 100, 20, 5, 1, now_ms - 60_000, now_ms - 2_000),
            ("s2", "p", None, "/home/me/old", "Last week", "x", 1, 1, 0, 0, now_ms - 10**9, now_ms - 10**9),
            ("s3", "p", "s1", "/home/me/portlin", "child", "x", 1, 1, 0, 0, now_ms, now_ms),
        ])
        process = agents.AgentProcess(pid=40, ppid=1, agent="opencode", command="opencode", cwd="/home/me/portlin",
                                      rss_bytes=0, cpu_seconds=0.0, started_at=0.0)
        sessions = agents.read_opencode_sessions(home, processes=[process], now=now_ms / 1000)
        assert [(s.title, s.pid, s.status) for s in sessions] == [("HUD work", 40, "working")]
        assert sessions[0].input_tokens == 100 and sessions[0].model == "anthropic/claude"

    def test_a_database_with_another_schema_is_an_empty_list(self, agents, tmp_path):
        database = agents.opencode_database(tmp_path)
        database.parent.mkdir(parents=True)
        sqlite3.connect(database).execute("CREATE TABLE other (x)").connection.close()
        assert agents.read_opencode_sessions(tmp_path, processes=[]) == []

    def test_no_database_is_an_empty_list(self, agents, tmp_path):
        assert agents.read_opencode_sessions(tmp_path, processes=[]) == []


class TestCollect:
    def test_an_empty_machine_collects_to_an_empty_report(self, agents, tmp_path):
        report = agents.collect(root=tmp_path, home=tmp_path, cache={}, now=1.0, dpkg_installed=set())
        assert report["processes"] == [] and report["sessions"] == [] and report["unmatched"] == []
        assert report["names"]["claude"] == "Claude Code"
        json.dumps(report)

    def test_a_claude_session_carries_the_limits_file(self, agents, tmp_path):
        home = tmp_path
        project = home / ".claude/projects/-home-me-portlin"
        project.mkdir(parents=True)
        (project / "s.jsonl").write_text(user("go") + "\n")
        (home / ".claude/abtop-rate-limits.json").write_text(
            '{"five_hour": {"used_percentage": 12, "resets_at": 1}, "updated_at": 5}')
        process = agents.AgentProcess(pid=20, ppid=1, agent="claude", command="claude", cwd="/home/me/portlin",
                                      rss_bytes=0, cpu_seconds=0.0, started_at=0.0)
        session = agents.read_claude_sessions(home, processes=[process], cache={}, now=100.0)[0]
        assert session.limits["primary"]["used_percent"] == 12.0

    def test_installed_agents_come_from_the_catalog(self, agents, tmp_path):
        (tmp_path / ".local/bin").mkdir(parents=True)
        (tmp_path / ".local/bin/codex").write_text("")
        names = agents.installed_agents(tmp_path, {"claude-code"})
        assert "Claude Code" in names and "Codex CLI" in names
        assert "Hermes Agent" not in names
