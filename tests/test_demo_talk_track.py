from __future__ import annotations

import re
from pathlib import Path

import pytest

DOC = Path(__file__).resolve().parent.parent / "docs" / "DEMO_SECURITY_AND_PEOPLE.md"

pytestmark = pytest.mark.skipif(not DOC.exists(), reason="the demo's presenter notes are local-only, not on this machine")


def _steps() -> list[tuple[str, str]]:
    """Each numbered step's heading and its Action line, in order."""
    text = DOC.read_text(encoding="utf-8")
    steps: list[tuple[str, str]] = []
    for match in re.finditer(r"^## (\d+\. .+)$\n+\*\*Action:\*\* (.+)$", text, re.MULTILINE):
        steps.append((match.group(1), match.group(2)))
    return steps


def test_the_steps_are_numbered_in_order() -> None:
    numbers = [int(heading.split(".", 1)[0]) for heading, _ in _steps()]
    assert numbers == list(range(1, len(numbers) + 1))


def test_after_an_admin_signs_in_the_notes_follow_the_portfolio_landing() -> None:
    steps = _steps()
    signed_in = [index for index, (_, action) in enumerate(steps) if "sign in with an admin account" in action.lower()]
    assert len(signed_in) == 1, "one step signs the admin in"
    admin = signed_in[0]
    assert "**Portfolio** opens" in steps[admin][1]
    overview = next(index for index in range(admin + 1, len(steps)) if "Overview" in steps[index][1])
    assert steps[overview][1].startswith("Click **Overview**"), steps[overview][0]
    for heading, action in steps[admin + 1:]:
        assert "sign in" not in action.lower() and "sign out" not in action.lower(), heading
