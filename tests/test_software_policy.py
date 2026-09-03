"""The polkit action the Software app elevates through.

A policy file is only load-bearing in its details: an action id the app does
not ask for, or an exec.path naming a program installed somewhere else, both
fail the same silent way. pkexec refuses, the app reports that nobody
answered, and nothing on screen says the two files disagree.

The facts are read out of the text rather than through an XML parser,
because CPython's parsers all go through pyexpat, which is a compiled
extension against a system library and does not import on every machine
these tests have to run on. Well-formedness is checked separately, and
skipped where the parser is unavailable; `make harness` proves the real
thing by asking polkit itself for the action on a booted Debian.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from portlin import package

RUNTIME = Path(__file__).resolve().parent.parent / "portlin" / "resources" / "runtime"
POLICY = RUNTIME / "org.portlin.install.policy"
TEXT = POLICY.read_text()


def element(name: str) -> str:
    match = re.search(rf"<{name}>(.*?)</{name}>", TEXT, re.DOTALL)
    assert match, f"no <{name}> in the policy"
    return match.group(1).strip()


def test_it_declares_the_action_the_app_asks_for():
    assert re.search(r'<action id="org\.portlin\.install">', TEXT)
    assert element("description")
    assert element("message")


def test_one_password_covers_a_few_installs():
    # auth_admin_keep rather than auth_admin: installing three things in a
    # row should ask once, not three times.
    assert element("allow_active") == "auth_admin_keep"
    assert element("allow_any") == "auth_admin"
    assert element("allow_inactive") == "auth_admin"


def test_it_grants_one_program_and_that_program_is_shipped():
    named = element_annotation("org.freedesktop.policykit.exec.path")
    assert named == "/usr/bin/portlin-install"
    # Named rather than assumed: this annotation is the whole of what the
    # action grants, so the path has to be one portlin-runtime installs, and
    # has to be executable once installed.
    assert named.lstrip("/") in package.text_files("portlin-runtime")
    assert named.lstrip("/") in package.executable_paths("portlin-runtime")


def element_annotation(key: str) -> str:
    match = re.search(rf'<annotate key="{re.escape(key)}">(.*?)</annotate>', TEXT)
    assert match, f"no annotation for {key}"
    return match.group(1).strip()


def test_the_prompt_carries_the_portlin_mark():
    # The name portlin-desktop installs into hicolor, so the authentication
    # dialog shows which program is asking rather than a placeholder.
    assert element("icon_name") == package.APP_ICON


def test_no_comment_ends_the_document_early():
    # A double hyphen inside an XML comment ends it at the parser but not at
    # the eye, and polkit skips a file it cannot parse without saying so.
    for comment in re.findall(r"<!--(.*?)-->", TEXT, re.DOTALL):
        assert "--" not in comment


def test_it_is_well_formed():
    # pyexpat rather than ElementTree: the wrapper imports fine and only
    # pulls the compiled parser in when asked to parse something.
    # exc_type, because the interesting failure here is not a missing module
    # but a present one that will not load against the system's expat.
    pytest.importorskip(
        "pyexpat",
        reason="this platform's Python cannot load pyexpat",
        exc_type=ImportError,
    )
    import xml.etree.ElementTree as ElementTree

    action = ElementTree.parse(POLICY).getroot().find("action")
    assert action.get("id") == "org.portlin.install"


def test_the_package_ships_it_where_polkit_reads_actions():
    destination = package.POLKIT_ACTIONS["org.portlin.install.policy"]
    assert destination == "usr/share/polkit-1/actions/org.portlin.install.policy"
    assert package.text_files("portlin-runtime")[destination] == TEXT
