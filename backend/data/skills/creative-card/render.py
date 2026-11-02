"""Deterministic placeholder renderer for ``template.md`` (neutral, no network)."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

PLACEHOLDER = re.compile(r"\{\{([a-z0-9_]+)\}\}")


def render(template_text: str, values: dict[str, str]) -> str:
    """Replace ``{{key}}`` placeholders; raise ``KeyError`` if a value is missing."""
    missing = {key for key in PLACEHOLDER.findall(template_text) if key not in values}
    if missing:
        raise KeyError(", ".join(sorted(missing)))
    return PLACEHOLDER.sub(lambda match: str(values[match.group(1)]), template_text)


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: render.py <template> <output> <values.json>", file=sys.stderr)
        return 2
    template_path, output_path, values_path = map(Path, argv[1:])
    values = json.loads(values_path.read_text(encoding="utf-8"))
    output = render(template_path.read_text(encoding="utf-8"), values)
    output_path.write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
