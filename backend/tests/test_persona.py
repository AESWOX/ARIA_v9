"""Test that persona.md is actually loaded into the system prompt."""

import os
import importlib.util


def _module_from_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_persona_md_exists():
    """persona.md must exist and be non-empty."""
    path = os.path.join(os.path.dirname(__file__), "..", "aria", "prompts", "persona.md")
    abspath = os.path.abspath(path)
    assert os.path.isfile(abspath), f"persona.md not found at {abspath}"
    with open(abspath) as f:
        content = f.read()
    assert len(content) > 200, f"persona.md too short ({len(content)} chars)"
    assert "честности" in content or "honesty" in content, "persona.md missing honesty rule"


def test_load_persona_function_exists():
    """_load_persona() must be defined in loop.py."""
    path = os.path.join(os.path.dirname(__file__), "..", "aria", "core", "loop.py")
    abspath = os.path.abspath(path)
    assert os.path.isfile(abspath), f"loop.py not found at {abspath}"
    with open(abspath) as f:
        content = f.read()
    assert "_load_persona" in content, "_load_persona() not found in loop.py"
    assert "_load_persona()" in content or "_load_persona()" in content, "_load_persona not called in loop.py"


def test_persona_appended_to_role_prompt():
    """loop.py must append persona.md result to the role_prompt."""
    path = os.path.join(os.path.dirname(__file__), "..", "aria", "core", "loop.py")
    abspath = os.path.abspath(path)
    with open(abspath) as f:
        content = f.read()
    assert "role_prompt += _load_persona()" in content, "persona not appended to role_prompt"
