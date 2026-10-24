"""The versioned snapshot corpus must stay complete and regression-stable."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentwriter.evaluation import (
    DEFAULT_CORPUS,
    CorpusError,
    compare_baseline,
    evaluate_snapshot,
    load_corpus,
)


def test_corpus_manifest_separates_snapshot_live_and_human_sampling() -> None:
    corpus = load_corpus(DEFAULT_CORPUS)
    assert len(corpus.cases) == 38
    assert len(corpus.live) == 16
    assert corpus.manifest["corpus_version"] == "1.0.0"
    assert corpus.manifest["snapshot_only"] is True
    assert corpus.manifest["counts"] == {
        "snapshot_entries": 38,
        "valid_snapshot_briefs": 32,
        "expected_rejections": 6,
        "live_entries_not_executed": 16,
    }
    assert corpus.manifest["human_sampling"]["sample_size"] == 10
    assert corpus.manifest["human_sampling"]["reviewers"] == 2
    assert corpus.manifest["editorial_rubric"]["file"] == "rubric.md"
    assert all(case.snapshot for case in corpus.cases)
    assert all(not case.snapshot for case in corpus.live)
    assert all(case.category != "creative_off" for case in corpus.live)


@pytest.mark.asyncio
async def test_every_snapshot_case_passes_and_matches_the_committed_baseline() -> None:
    result = await evaluate_snapshot(DEFAULT_CORPUS)
    summary = result["summary"]
    assert summary["valid_cases"] == 32
    assert summary["rejection_cases"] == 6
    assert summary["checks_total"] == summary["checks_passed"]
    assert summary["failed"] == 0
    assert summary["all_passed"] is True
    assert result["mode"] == "snapshot_fixture"
    assert result["live"]["status"] == "not_executed"
    assert len(result["cases"]) == 38
    comparison = compare_baseline(result, DEFAULT_CORPUS / "baseline.json")
    assert comparison["passed"], comparison["differences"]


def test_modified_corpus_file_is_rejected_before_evaluation(tmp_path: Path) -> None:
    copied = tmp_path / "v1"
    shutil.copytree(DEFAULT_CORPUS, copied)
    briefs = copied / "briefs.jsonl"
    briefs.write_text(briefs.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(CorpusError, match="manifest hash"):
        load_corpus(copied)
