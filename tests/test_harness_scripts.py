"""Checks on the container harnesses in scripts/.

These need root, a real dpkg and a Debian userland, so nothing here runs them.
What can be verified is the one property they cannot assert about themselves:
that each is independent of the container state the previous one leaves behind.
`make harness` runs them in sequence in a single container, so a script that
assumes a clean slate is a script that breaks whenever an earlier one changes
-- or, as happened here, whenever the version in portlin/__init__.py moves.
"""

from __future__ import annotations

from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
UPGRADE = SCRIPTS / "test-package-upgrade.py"


class TestUpgradeHarnessStartsFromNothing:
    """test-package-conflicts.py runs first and installs portlin's packages at
    the working tree's own version, removing only portlin-desktop afterwards.
    This harness then installs a deliberately older v1 over what is left, and
    apt refuses a downgrade -- so its hardcoded versions would otherwise have
    to be bumped in step with every release, silently, on pain of a red gate.
    """

    def test_it_parses(self):
        compile(UPGRADE.read_text(), str(UPGRADE), "exec")

    def test_it_clears_portlin_packages_before_the_first_install(self):
        source = UPGRADE.read_text()
        main_body = source[source.index("def main"):]
        assert "clear_previous_installs()" in main_body, (
            "nothing removes what an earlier harness installed"
        )
        assert main_body.index("clear_previous_installs()") < main_body.index(
            "result = install(debs)"
        )

    def test_it_purges_rather_than_merely_removing(self):
        # dpkg -r leaves conffiles on disk. This harness exists to assert what
        # happens to conffiles across an upgrade, so starting with somebody
        # else's copies of them already installed proves nothing.
        source = UPGRADE.read_text()
        body = source[source.index("def clear_previous_installs"):source.index("def main")]
        assert "--purge" in body

    def test_it_clears_every_package_portlin_ships(self):
        # Derived from package.PACKAGES rather than a hand-written list, so a
        # fourth package cannot be left installed and silently reintroduce the
        # downgrade this guards against.
        source = UPGRADE.read_text()
        body = source[source.index("def clear_previous_installs"):source.index("def main")]
        assert "PACKAGES" in body
