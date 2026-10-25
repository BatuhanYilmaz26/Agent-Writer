"""Check the Git index for private documents, local state and credential material.

Run after staging and before publishing. Only indexed bytes are inspected, so
ignored local documents and credentials are never read by this check.
"""

from __future__ import annotations

import argparse
import fnmatch
import re
import subprocess
from pathlib import Path, PurePosixPath

PUBLIC_DOCUMENTS = frozenset({
    "readme.md", "docs/user-guide.md", "docs/setup-and-operations.md",
    "docs/configuration.md", "docs/workflow.md", "docs/providers.md", "docs/budgets.md",
    "evaluation/corpus/v1/rubric.md", "evaluation/corpus/v2/rubric.md",
})
PRIVATE_PARTS = frozenset({
    ".local", ".secrets", ".venv", "venv", ".agents", ".codex", ".opencode",
    ".aws", ".ssh", ".gnupg",
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
})
PRIVATE_ROOTS = frozenset({"internal", "private", "notes", "plans", "reports", "evidence", "archives", "backups"})
PRIVATE_NAMES = (
    ".env", ".env.*", "*.env", "*.env.*", "*.pem", "*.key", "*.p12", "*.pfx",
    "*.jks", "*.bundle", "*.db", "*.sqlite*", "*.dump", "*.dump.*", "*.backup", "*.backup.*", "*.log",
    "credentials.json", "service-account*.json", "id_rsa", "id_ed25519",
    "opencode*.json", "opencode*.jsonc", ".npmrc", ".pypirc", ".netrc", ".pgpass",
)
SECRET_PATTERNS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    "GitHub token": re.compile(rb"(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})"),
    "provider token": re.compile(rb"sk-(?:proj-|or-v1-)?[A-Za-z0-9_-]{24,}"),
    "AWS access key": re.compile(rb"(?:AKIA|ASIA)[A-Z0-9]{16}"),
}


def private_path(path: str) -> bool:
    normalized = PurePosixPath(path.lower())
    if any(part in PRIVATE_PARTS for part in normalized.parts):
        return True
    if normalized.parts[0] in PRIVATE_ROOTS:
        return True
    name = normalized.name
    if str(normalized) not in PUBLIC_DOCUMENTS:
        if normalized.parts[0] == "docs":
            return True
        if normalized.suffix in {".md", ".mdx", ".rst", ".docx", ".pdf"}:
            return True
    if name == ".env.example":
        return False
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in PRIVATE_NAMES)


def credential_kinds(content: bytes) -> list[str]:
    """Return labels only; never include a matched value or source line."""
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(content)]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", action="store_true", help="Check current and untracked nonignored files instead")
    args = parser.parse_args()
    command = ["git", "ls-files", "--cached", "-z"]
    if args.worktree:
        command.extend(["--others", "--exclude-standard"])
    paths = subprocess.check_output(command).split(b"\0")
    findings: list[str] = []
    checked = 0
    for raw_path in paths:
        if not raw_path:
            continue
        path = raw_path.decode("utf-8")
        checked += 1
        if private_path(path):
            findings.append(f"Private path is staged: {path}")
            continue
        content = Path(path).read_bytes() if args.worktree else subprocess.check_output(["git", "show", ":" + path])
        for kind in credential_kinds(content):
            findings.append(f"Possible {kind} in staged file: {path}")
    if findings:
        print("Publication check failed. Unstage private files or remove credentials:")
        for finding in findings:
            print("- " + finding)
        return 1
    scope = "working-tree" if args.worktree else "staged"
    print(f"Publication check passed: {checked} {scope} files; no private paths or token/key patterns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
