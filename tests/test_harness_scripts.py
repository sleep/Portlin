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
SOFTWARE = SCRIPTS / "test-software.py"
STASH = SCRIPTS / "test-stash-passphrase.py"
EXPAND = SCRIPTS / "test-expand.py"


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


class TestSoftwareHarness:
    """test-software.py drives the shipped installer against a real archive.

    It runs before test-package-upgrade.py, which purges portlin's packages
    first, so what it installs is that harness's problem rather than a
    trap. What it must not do is leave the catalog's own entries installed:
    an entry left behind would make a later run's "install it, check it is
    there" prove nothing.
    """

    def test_it_parses(self):
        compile(SOFTWARE.read_text(), str(SOFTWARE), "exec")

    def test_the_entries_it_drives_are_in_the_catalog(self):
        # Named as ids rather than package names, and checked against the
        # shipped catalog, so renaming an entry cannot leave the harness
        # driving something that no longer exists.
        import sys

        runtime = SCRIPTS.parent / "portlin" / "resources" / "runtime"
        if str(runtime) not in sys.path:
            sys.path.insert(0, str(runtime))
        import catalog

        source = SOFTWARE.read_text()
        for line in source.splitlines():
            if line.startswith(("DEBIAN_ENTRY =", "VENDOR_ENTRY =")):
                entry_id = line.split("=")[1].strip().strip('"')
                assert catalog.by_id(entry_id)

    def test_it_removes_everything_it_installs(self):
        source = SOFTWARE.read_text()
        body = source[source.index("def check_install_and_remove"):source.index("def check_privilege")]
        assert body.index('"install", entry_id') < body.index('"remove", entry_id')

    def test_it_pins_no_package_version(self):
        # Versions move with every Debian point release. A harness that named
        # one would go red on somebody else's schedule.
        import re

        for line in SOFTWARE.read_text().splitlines():
            assert not re.search(r'"[a-z0-9-]+=[0-9]+[.:]', line), line

    def test_it_asks_polkit_rather_than_trusting_the_file(self):
        # A malformed policy file is skipped in silence, so parsing it here
        # would prove only that this harness can parse it.
        assert "pkaction" in SOFTWARE.read_text()

    def test_a_vendor_outage_does_not_fail_the_gate(self):
        # The Debian entry is portlin's own correctness. The vendor entry
        # depends on somebody else's CDN being up, and a red gate for that
        # teaches people to ignore the gate.
        source = SOFTWARE.read_text()
        body = source[source.index("def check_install_and_remove"):source.index("def check_privilege")]
        assert "skip(" in body


class TestStashHarnessOwnsNothingItDidNotMake:
    """/run/portlin is shared, and test-encrypt-hook.py runs first.

    That harness leaves a `just-encrypted` breadcrumb in the same directory
    this one has to replace with a plain file, so a cleanup that assumed an
    empty directory failed on the harness before it rather than on anything
    it was testing.
    """

    def test_it_clears_the_directory_rather_than_assuming_it_is_empty(self):
        source = STASH.read_text()
        assert "rmtree" in source
        assert ".parent.rmdir()" not in source


class TestExpandHarnessLoadsTheWizardAsWritten:
    """The point of this harness is that it runs the wizard's real code.

    It execs the expansion functions out of the shipped file, so anything
    those functions reference has to be in the namespace too. Hand-listing
    the names goes stale in silence: the expansion path grew a reference to
    the passphrase stash, and the harness then failed with a NameError
    inside code that is correct on a real stick.
    """

    def test_it_executes_the_wizards_own_constants(self):
        source = EXPAND.read_text()
        body = source[source.index("def wizard_functions"):source.index("def run_packaged_expand")]
        assert 'source[:source.index("\\ndef ")]' in body

    def test_every_name_the_expansion_path_uses_resolves(self):
        # The check the harness cannot make about itself until it is too
        # late: take the same slice it takes, and require every global that
        # slice reads to be either a builtin, something the harness seeds, or
        # something the wizard's own header defines.
        import ast
        import builtins

        wizard = (
            Path(__file__).resolve().parent.parent
            / "portlin" / "resources" / "firstboot" / "portlin-firstboot"
        )
        source = wizard.read_text()
        header = source[: source.index("\ndef ")]
        namespace: dict = {}
        exec(compile(header, str(wizard), "exec"), namespace)
        seeded = {"Path", "re", "os", "subprocess", "log", "ask_password", "message", "run"}
        known = set(namespace) | seeded | set(dir(builtins))

        block = source[source.index("def _resize_mapping"):source.index("def step_autologin")]
        tree = ast.parse(block)
        bound = {node.name for node in ast.walk(tree)
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
        bound |= {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
        bound |= {argument.arg for node in ast.walk(tree)
                  if isinstance(node, ast.arguments)
                  for argument in node.posonlyargs + node.args + node.kwonlyargs}
        bound |= {node.name for node in ast.walk(tree)
                  if isinstance(node, ast.ExceptHandler) and node.name}
        loaded = {node.id for node in ast.walk(tree)
                  if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}

        missing = sorted(loaded - bound - known)
        assert not missing, f"the expansion path reads names nothing defines: {missing}"
