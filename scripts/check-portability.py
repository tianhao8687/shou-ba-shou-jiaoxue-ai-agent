from __future__ import annotations

from pathlib import Path
import re
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRECTORIES = {
    ".git",
    ".models",
    ".runtime",
    ".venv",
    "__pycache__",
    "dist",
    "node_modules",
}
TEXT_SUFFIXES = {
    ".css",
    ".csv",
    ".env",
    ".html",
    ".js",
    ".json",
    ".md",
    ".ps1",
    ".py",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}
TEXT_FILENAMES = {
    ".dockerignore",
    ".env.example",
    ".gitattributes",
    ".gitignore",
    "Dockerfile",
}
PERSONAL_PATH_PATTERNS = {
    "Windows absolute path": re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]"),
    "POSIX user-home path": re.compile(r"(?<![A-Za-z0-9])/(?:home|Users)/[^/\s]+/"),
}


def candidate_files() -> list[Path]:
    files: list[Path] = []
    for path in REPOSITORY_ROOT.rglob("*"):
        relative = path.relative_to(REPOSITORY_ROOT)
        if any(part in SKIPPED_DIRECTORIES for part in relative.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in TEXT_SUFFIXES or path.name in TEXT_FILENAMES:
            files.append(path)
    return sorted(files)


def main() -> int:
    violations: list[str] = []
    checked = 0
    for path in candidate_files():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        relative = path.relative_to(REPOSITORY_ROOT).as_posix()
        for line_number, line in enumerate(text.splitlines(), start=1):
            for label, pattern in PERSONAL_PATH_PATTERNS.items():
                if pattern.search(line):
                    violations.append(f"{relative}:{line_number}: {label}")

    if violations:
        print("Portability check failed:", file=sys.stderr)
        for violation in violations:
            print(f"- {violation}", file=sys.stderr)
        return 1

    print(f"Portability check passed: {checked} text files contain no personal absolute paths.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
