"""Check local documentation links, code fences and release version consistency."""

import ast
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
EXCLUDED = {
    ".git",
    ".tools",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "var",
    "output",
    "dist",
    "build",
    "test-results",
    "playwright-report",
}


def main() -> int:
    errors = []
    count = 0
    for directory, children, files in os.walk(ROOT):
        children[:] = [name for name in children if name not in EXCLUDED]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = Path(directory) / name
            text = path.read_text(encoding="utf-8")
            count += 1
            fenced = False
            for line_number, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("```"):
                    fenced = not fenced
                    continue
                if fenced:
                    continue
                for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", line):
                    target = match.group(1).strip().split(' "', 1)[0].strip("<>")
                    url = urlsplit(target)
                    if url.scheme or url.netloc or not url.path:
                        continue
                    if not (path.parent / unquote(url.path)).exists():
                        errors.append(f"{path.relative_to(ROOT)}:{line_number}: missing {target}")
            if fenced:
                errors.append(f"{path.relative_to(ROOT)}: unclosed code fence")

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    tree = ast.parse((ROOT / "src/after_sales/__init__.py").read_text())
    runtime_version = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets
        )
    )
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    locked_version = next(p["version"] for p in lock["package"] if p["name"] == project["name"])
    if project["version"] != runtime_version or locked_version != runtime_version:
        errors.append("pyproject, uv.lock and runtime versions differ")
    for error in errors:
        print(error)
    if errors:
        return 1
    print(f"Documentation: {count} Markdown files, local links and fences OK; v{runtime_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
