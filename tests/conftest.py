from __future__ import annotations

import pytest

from portlin.runner import Runner


@pytest.fixture
def runner() -> Runner:
    """A Runner that records commands instead of running them.

    This is what makes the destructive orchestration testable off Linux: the
    whole of write_stick and build_rootfs can be replayed and asserted on as a
    list of commands, on any machine, with no root and no block devices.
    """
    return Runner(dry_run=True)


class Trace:
    """Query helper over a recorded command list."""

    def __init__(self, runner: Runner) -> None:
        self.commands = runner.commands
        self.lines = runner.rendered()

    def index(self, *fragments: str) -> int:
        """Position of the first command containing every fragment."""
        for position, line in enumerate(self.lines):
            if all(fragment in line for fragment in fragments):
                return position
        raise AssertionError(
            f"no command matched {fragments!r}.\nRecorded:\n  "
            + "\n  ".join(self.lines)
        )

    def last_index(self, *fragments: str) -> int:
        for position in reversed(range(len(self.lines))):
            if all(fragment in self.lines[position] for fragment in fragments):
                return position
        raise AssertionError(f"no command matched {fragments!r}")

    def has(self, *fragments: str) -> bool:
        return any(
            all(fragment in line for fragment in fragments) for line in self.lines
        )

    def count(self, *fragments: str) -> int:
        return sum(
            1 for line in self.lines
            if all(fragment in line for fragment in fragments)
        )

    def before(self, first: tuple[str, ...], second: tuple[str, ...]) -> bool:
        return self.index(*first) < self.index(*second)

    # Token-exact variants. Substring matching is convenient but matches against
    # the whole rendered line including paths, and pytest's tmp_path embeds the
    # test's own name -- so a test called ...installs... makes every line look
    # like it contains "install". Where the thing being matched is an argv token,
    # match it as one.

    def token_index(self, *tokens: str) -> int:
        for position, command in enumerate(self.commands):
            if all(token in command for token in tokens):
                return position
        raise AssertionError(
            f"no command had tokens {tokens!r}.\nRecorded:\n  "
            + "\n  ".join(self.lines)
        )

    def has_tokens(self, *tokens: str) -> bool:
        return any(all(token in command for token in tokens) for command in self.commands)

    def tokens_before(self, first: tuple[str, ...], second: tuple[str, ...]) -> bool:
        return self.token_index(*first) < self.token_index(*second)

    def command_at(self, *tokens: str) -> list[str]:
        return self.commands[self.token_index(*tokens)]


@pytest.fixture
def trace():
    return Trace
