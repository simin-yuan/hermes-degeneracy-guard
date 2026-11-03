#!/usr/bin/env python3
"""Write Hermes's ``[tool.uv].override-dependencies`` to a requirements file.

``uv pip install`` is the pip-compatible interface: it reads ``[project]`` but not
``[tool.uv]``, so Hermes's overrides do not apply to it. Hermes relies on one of them:
``browser-harness==0.1.13`` pins ``pillow==12.3.0``, while Hermes itself pins
``Pillow==13.0.0`` to pick up the advisories fixed in 13. Installing without the override
cannot resolve at all ("No solution found when resolving dependencies").

Usage: hermes-overrides.py <output-file>
"""

import pathlib
import sys
import tomllib


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: hermes-overrides.py <output-file>", file=sys.stderr)
        return 2

    project = pathlib.Path(".hermes-agent/pyproject.toml")
    if not project.is_file():
        print(f"{project} not found; run from the repository root", file=sys.stderr)
        return 2

    data = tomllib.loads(project.read_text(encoding="utf-8"))
    overrides = data.get("tool", {}).get("uv", {}).get("override-dependencies", [])

    out = pathlib.Path(sys.argv[1])
    out.write_text("\n".join(overrides) + "\n", encoding="utf-8")
    print(f"{len(overrides)} override(s) written to {out}")
    for override in overrides:
        print(f"  {override}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
