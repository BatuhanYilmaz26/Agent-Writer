import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "publication_check", Path(__file__).resolve().parents[2] / "scripts/check_publication.py",
)
assert spec is not None and spec.loader is not None
publication = importlib.util.module_from_spec(spec)
spec.loader.exec_module(publication)


@pytest.mark.parametrize("path", [
    "IMPLEMENTATION_TRACKER.md", "PLANNING_REVIEW.md", "fieldnote-production-guide-v3.md",
    "docs/evidence/report.json", "docs/archive/original.md", "docs/runbooks/alerts.md",
    "nested/PRIVATE.MD", "internal/report.json", ".local/before.bundle", ".secrets/treg.token",
    "opencode.json", "nested/.env.production", "nested/production.env", "credentials.json",
    "backups/snapshot.dump.enc", "nested/snapshot.dump.enc", ".aws/credentials",
    "compose.override.yaml", "compose.override.yml",
])
def test_private_paths_are_rejected_even_if_force_staged(path):
    assert publication.private_path(path)


@pytest.mark.parametrize("path", [
    "README.md", "docs/user-guide.md", "docs/providers.md", "src/fieldnote/main.py",
    "tests/unit/test_settings.py", ".env.example", "requirements.lock",
    "evaluation/corpus/v1/rubric.md", "evaluation/corpus/v2/rubric.md", "migrations/0001_initial.sql",
])
def test_reviewed_product_files_remain_publishable(path):
    assert not publication.private_path(path)


def test_credential_check_returns_only_a_label_never_the_matched_secret():
    token = b"ghp_" + b"x" * 36
    labels = publication.credential_kinds(b"key = '" + token + b"'")
    assert labels == ["GitHub token"]
    assert token.decode() not in str(labels)
