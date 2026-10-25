"""Deterministic quality evaluation for the versioned brief corpus.

Snapshot mode runs every valid brief through the production graph assembly path
with the official fixture dependencies. It checks structural completeness,
citation identity, word limits, review gates, disclosure and the research
boundary. It does not qualify factual accuracy or a live provider; those remain
QAL-03 and live-provider gates.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

from pydantic import ValidationError

from fieldnote.domain import CreateRunRequest
from fieldnote.graph import build_graph, initial_state, make_fixture_dependencies
from fieldnote.services.citations import CitationError, validate_citations

EVALUATOR_VERSION = "2.0.1"
GRAPH_VERSION = "v1"
DISCLOSURE = "> Synthetic development draft. No live model or web research was used."
DEFAULT_CORPUS = Path(__file__).resolve().parents[2] / "evaluation" / "corpus" / "v2"
REQUIRED_FILES = ("briefs.jsonl", "live.jsonl", "rubric.md")


class CorpusError(ValueError):
    """The versioned corpus is missing, inconsistent or has been modified."""


@dataclass(frozen=True, slots=True)
class BriefCase:
    id: str
    category: str
    snapshot: bool
    request: dict[str, Any]
    expected: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Corpus:
    manifest: dict[str, Any]
    cases: tuple[BriefCase, ...]
    live: tuple[BriefCase, ...]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CorpusError(f"{name} must be an object")
    return value


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CorpusError(f"{name} must be an integer")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CorpusError(f"{name} must be a non-empty string")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CorpusError(f"{path.name} line {line_number}: invalid JSON") from exc
        entries.append(_mapping(raw, f"{path.name} line {line_number}"))
    return entries


def _parse_case(raw: dict[str, Any], *, snapshot: bool) -> BriefCase:
    identifier = _text(raw.get("id"), "case id")
    category = _text(raw.get("category"), f"{identifier} category")
    if raw.get("snapshot") is not snapshot:
        raise CorpusError(f"{identifier}: snapshot flag must be {snapshot}")
    request = _mapping(raw.get("request"), f"{identifier} request")
    expected = _mapping(raw.get("expected"), f"{identifier} expected")
    if "validation_error" not in expected and expected.get("outcome") != "needs_review":
        raise CorpusError(f"{identifier}: valid fixture cases must expect needs_review")
    return BriefCase(
        id=identifier,
        category=category,
        snapshot=snapshot,
        request=request,
        expected=expected,
    )


def load_corpus(directory: Path = DEFAULT_CORPUS) -> Corpus:
    """Load and verify a versioned corpus before any evaluation runs."""
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise CorpusError(f"missing corpus manifest: {manifest_path}")
    try:
        manifest = _mapping(json.loads(manifest_path.read_text(encoding="utf-8")), "manifest")
    except json.JSONDecodeError as exc:
        raise CorpusError("manifest.json is not valid JSON") from exc

    files = _mapping(manifest.get("files"), "manifest files")
    for name in REQUIRED_FILES:
        info = _mapping(files.get(name), f"manifest files.{name}")
        path = directory / name
        if not path.is_file():
            raise CorpusError(f"missing corpus file: {path}")
        expected_hash = _text(info.get("sha256"), f"manifest files.{name}.sha256")
        actual_hash = _sha256(path)
        if actual_hash != expected_hash:
            raise CorpusError(f"{name} does not match its manifest hash")

    cases = tuple(
        _parse_case(raw, snapshot=True) for raw in _read_jsonl(directory / "briefs.jsonl")
    )
    live = tuple(
        _parse_case(raw, snapshot=False) for raw in _read_jsonl(directory / "live.jsonl")
    )
    if not cases:
        raise CorpusError("briefs.jsonl is empty")
    if len({case.id for case in cases}) != len(cases):
        raise CorpusError("briefs.jsonl contains duplicate ids")
    if len({case.id for case in live}) != len(live):
        raise CorpusError("live.jsonl contains duplicate ids")
    if {case.id for case in cases} & {case.id for case in live}:
        raise CorpusError("snapshot and live ids overlap")

    counts = _mapping(manifest.get("counts"), "manifest counts")
    expected_counts = {
        "snapshot_entries": len(cases),
        "valid_snapshot_briefs": sum(
            1 for case in cases if "validation_error" not in case.expected
        ),
        "expected_rejections": sum(
            1 for case in cases if "validation_error" in case.expected
        ),
        "live_entries_not_executed": len(live),
    }
    for name, expected in expected_counts.items():
        actual = _integer(counts.get(name), f"manifest counts.{name}")
        if actual != expected:
            raise CorpusError(f"manifest count {name} is {actual}, corpus has {expected}")

    distribution = Counter(case.category for case in cases)
    declared = _mapping(manifest.get("distribution"), "manifest distribution")
    if dict(sorted(distribution.items())) != {
        str(key): _integer(value, f"manifest distribution.{key}")
        for key, value in declared.items()
    }:
        raise CorpusError("manifest distribution does not match briefs.jsonl")

    return Corpus(manifest=manifest, cases=cases, live=live)


def _add(checks: list[dict[str, Any]], name: str, passed: bool, detail: str) -> None:
    checks.append({"name": name, "passed": bool(passed), "detail": detail})


def _case_result(
    case: BriefCase,
    *,
    kind: str,
    checks: list[dict[str, Any]],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    failures = [check for check in checks if not check["passed"]]
    return {
        "id": case.id,
        "category": case.category,
        "kind": kind,
        "status": "pass" if not failures else "fail",
        "checks": checks,
        "metrics": metrics,
        "failures": [f"{check['name']}: {check['detail']}" for check in failures],
    }


def _evaluate_rejection(case: BriefCase) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    expected_field = str(case.expected.get("field") or "")
    expected_marker = str(case.expected.get("marker") or "")
    try:
        CreateRunRequest(**case.request)
    except ValidationError as exc:
        errors = exc.errors()
        fields = {str(error["loc"][0]) for error in errors if error["loc"]}
        rendered = str(exc)
        _add(
            checks,
            "rejected",
            True,
            "CreateRunRequest raised ValidationError as expected",
        )
        _add(
            checks,
            "field",
            expected_field in fields if expected_field else True,
            f"expected field {expected_field or '<model>'}, saw {sorted(fields)}",
        )
        _add(
            checks,
            "marker",
            expected_marker in rendered if expected_marker else True,
            f"expected message containing {expected_marker!r}",
        )
        metrics: dict[str, Any] = {
            "validation_errors": [
                {
                    "field": str(error["loc"][0]) if error["loc"] else "<model>",
                    "type": error["type"],
                }
                for error in errors
            ]
        }
    else:
        _add(checks, "rejected", False, "CreateRunRequest accepted an invalid brief")
        metrics = {"validation_errors": []}
    return _case_result(case, kind="rejection", checks=checks, metrics=metrics)


async def _evaluate_valid(case: BriefCase) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    try:
        request = CreateRunRequest(**case.request)
    except ValidationError as exc:
        _add(checks, "request_validation", False, f"unexpected ValidationError: {exc}")
        return _case_result(
            case,
            kind="valid",
            checks=checks,
            metrics={"validation_error": True},
        )
    _add(checks, "request_validation", True, "CreateRunRequest accepted the brief")

    expected_research = bool(case.expected.get("research"))
    expected_unsupported = _integer(
        case.expected.get("unsupported_claims", 0), "expected unsupported_claims"
    )
    expected_sections = _integer(case.expected.get("sections", 2), "expected sections")

    graph = build_graph(make_fixture_dependencies(request)).compile()
    result: dict[str, Any] = await graph.ainvoke(
        initial_state(), {"recursion_limit": 50}
    )

    final = str(result.get("final_markdown") or "")
    assembled = str(result.get("assembled_markdown") or "")
    plan = _mapping(result.get("plan") or {}, "graph plan")
    tasks = plan.get("tasks") or []
    sections = result.get("sections") or {}
    latest = _integer(result.get("revision"), "graph revision")
    decision = _mapping(result.get("decision") or {}, "graph decision")
    sources = _mapping(result.get("sources") or {}, "graph sources")
    claims = result.get("claims") or []
    issues = result.get("issues") or []
    review = _mapping(result.get("review") or {}, "graph review")
    issue_codes = [
        str(issue.get("code")) for issue in issues if isinstance(issue, dict)
    ]

    # Outcome and research policy are decided by the graph, not inferred from
    # model prose.
    _add(
        checks,
        "outcome",
        result.get("outcome") == case.expected.get("outcome"),
        f"expected {case.expected.get('outcome')!r}, saw {result.get('outcome')!r}",
    )
    actual_research = decision.get("need_research")
    expected_mode = "required" if expected_research else "off"
    _add(
        checks,
        "research_policy",
        actual_research is expected_research
        and decision.get("effective_mode") == expected_mode,
        f"expected research={expected_research} mode={expected_mode!r}, "
        f"saw research={actual_research!r} mode={decision.get('effective_mode')!r}",
    )

    model_calls = result.get("model_calls")
    fixture_calls = result.get("fixture_calls")
    _add(
        checks,
        "fixture_provenance",
        final.startswith(DISCLOSURE)
        and isinstance(model_calls, int)
        and model_calls > 0
        and model_calls == fixture_calls,
        f"disclosure={final.startswith(DISCLOSURE)} model_calls={model_calls!r} "
        f"fixture_calls={fixture_calls!r}",
    )

    missing_sections = [
        f"{task.get('id')}:{latest}"
        for task in tasks
        if isinstance(task, dict) and f"{task.get('id')}:{latest}" not in sections
    ]
    heading_counts = [
        final.count(f"## {task.get('title')}")
        for task in tasks
        if isinstance(task, dict) and task.get("title")
    ]
    _add(
        checks,
        "completeness",
        len(tasks) == expected_sections
        and not missing_sections
        and bool(heading_counts)
        and all(count == 1 for count in heading_counts)
        and bool(plan.get("title")),
        f"tasks={len(tasks)} expected={expected_sections} missing={missing_sections} "
        f"heading_counts={heading_counts} title={plan.get('title')!r}",
    )

    body = final.split("## References", 1)[0]
    prose = "\n".join(
        line
        for line in body.splitlines()
        if not line.lstrip().startswith(("#", ">"))
    )
    prose = re.sub(r"\[@[^\]]+\]|\[\d+\]", "", prose)
    word_count = len(prose.split())
    tolerance = max(50, round(request.target_words * 0.10))
    _add(
        checks,
        "word_limit",
        abs(word_count - request.target_words) <= tolerance,
        f"prose words={word_count} target={request.target_words} tolerance=+/-{tolerance}",
    )

    citation_ids: tuple[str, ...] = ()
    citation_count = 0
    reference_count = len(re.findall(r"(?m)^\d+\.\s+\[", final))
    unresolved_marker = "[@" in final
    try:
        validation = validate_citations(
            assembled, sources, require_citations=expected_research
        )
        citation_ids = validation.source_ids
        citation_count = validation.citation_count
        citation_error = ""
    except CitationError as exc:
        citation_error = str(exc)
    if expected_research:
        reference_passed = (
            not citation_error
            and bool(sources)
            and citation_count > 0
            and "## References" in final
            and reference_count == len(citation_ids)
            and not unresolved_marker
        )
    else:
        reference_passed = (
            not citation_error
            and not sources
            and citation_count == 0
            and "## References" not in final
            and not unresolved_marker
        )
    _add(
        checks,
        "references",
        reference_passed,
        f"sources={len(sources)} citation_ids={list(citation_ids)} "
        f"citations={citation_count} references={reference_count} "
        f"unresolved={unresolved_marker} error={citation_error or 'none'}",
    )

    unsupported = sum(
        1
        for claim in claims
        if isinstance(claim, dict) and claim.get("assessment") == "unsupported"
    )
    if expected_research:
        claim_passed = (
            unsupported == expected_unsupported
            and unsupported > 0
            and issue_codes.count("unsupported_claim") == unsupported
        )
    else:
        claim_passed = (
            not claims
            and "unresearched_draft" in issue_codes
            and unsupported == 0
        )
    _add(
        checks,
        "claim_assessment",
        claim_passed,
        f"unsupported={unsupported} expected={expected_unsupported} "
        f"issue_codes={sorted(set(issue_codes))}",
    )

    _add(
        checks,
        "review_gate",
        result.get("outcome") == "needs_review"
        and result.get("review_status") == "needs_review"
        and review.get("approved") is False
        and "synthetic_fixture" in issue_codes,
        f"outcome={result.get('outcome')!r} review_status={result.get('review_status')!r} "
        f"approved={review.get('approved')!r} synthetic_fixture="
        f"{'synthetic_fixture' in issue_codes}",
    )

    unsafe_markup = bool(re.search(r"</?[A-Za-z][^>]*>|<!--", body))
    authored_link = bool(re.search(r"!?\[[^\]]*\]\s*\(", body))
    credential_like = bool(
        re.search(
            r"\bsk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._~+/=-]+|"
            r"OPENROUTER_API_KEY|TREG_TOKEN|postgres(?:ql)?://",
            final,
        )
    )
    _add(
        checks,
        "safety",
        not unsafe_markup
        and not authored_link
        and not credential_like
        and len(final) <= 200_000,
        f"unsafe_markup={unsafe_markup} authored_link={authored_link} "
        f"credential_like={credential_like} length={len(final)}",
    )

    if case.category == "adversarial_valid":
        instructions = str(case.request.get("instructions") or "").strip()
        echoed = bool(instructions) and instructions in final
        _add(
            checks,
            "adversarial_containment",
            not echoed,
            f"instructions echoed verbatim={echoed}; fixture output remains structured",
        )

    metrics = {
        "outcome": result.get("outcome"),
        "research": actual_research,
        "effective_mode": decision.get("effective_mode"),
        "word_count": word_count,
        "target_words": request.target_words,
        "section_count": len(tasks),
        "citation_count": citation_count,
        "source_count": len(sources),
        "unsupported_claims": unsupported,
        "issue_codes": sorted(set(issue_codes)),
        "model_calls": model_calls,
        "fixture_calls": fixture_calls,
    }
    return _case_result(case, kind="valid", checks=checks, metrics=metrics)


async def evaluate_snapshot(
    corpus_dir: Path = DEFAULT_CORPUS,
    *,
    baseline_path: Path | None = None,
) -> dict[str, Any]:
    """Evaluate every snapshot case and return a deterministic result manifest."""
    corpus = load_corpus(corpus_dir)
    started = monotonic()
    cases: list[dict[str, Any]] = []
    for case in corpus.cases:
        if "validation_error" in case.expected:
            cases.append(_evaluate_rejection(case))
        else:
            cases.append(await _evaluate_valid(case))
    summary = _summarize(cases)
    result: dict[str, Any] = {
        "evaluator_version": EVALUATOR_VERSION,
        "graph_version": GRAPH_VERSION,
        "corpus_version": corpus.manifest.get("corpus_version"),
        "mode": "snapshot_fixture",
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(monotonic() - started, 3),
        "corpus_files": {
            name: _sha256(Path(corpus_dir) / name) for name in REQUIRED_FILES
        },
        "summary": summary,
        "cases": cases,
        "live": {
            "status": "not_executed",
            "entries": len(corpus.live),
            "reason": (
                "Live provider qualification requires qualified OpenRouter and Treg "
                "credentials, budget approval and QAL-03 human review. The snapshot "
                "evaluator never calls a live provider."
            ),
        },
        "result_hash": "",
    }
    result["result_hash"] = _result_hash(summary, cases)
    if baseline_path is not None:
        result["baseline"] = compare_baseline(result, baseline_path)
    return result


def _summarize(cases: list[dict[str, Any]]) -> dict[str, Any]:
    passed = sum(1 for case in cases if case["status"] == "pass")
    checks_total = sum(len(case["checks"]) for case in cases)
    checks_passed = sum(
        1 for case in cases for check in case["checks"] if check["passed"]
    )
    categories: dict[str, dict[str, int]] = {}
    for case in cases:
        bucket = categories.setdefault(case["category"], {"total": 0, "passed": 0})
        bucket["total"] += 1
        bucket["passed"] += 1 if case["status"] == "pass" else 0
    return {
        "total_entries": len(cases),
        "valid_cases": sum(1 for case in cases if case["kind"] == "valid"),
        "rejection_cases": sum(1 for case in cases if case["kind"] == "rejection"),
        "passed": passed,
        "failed": len(cases) - passed,
        "all_passed": passed == len(cases),
        "checks_total": checks_total,
        "checks_passed": checks_passed,
        "categories": dict(sorted(categories.items())),
    }


def _result_hash(summary: dict[str, Any], cases: list[dict[str, Any]]) -> str:
    payload = json.dumps(
        {"summary": summary, "cases": cases},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compare_baseline(result: dict[str, Any], baseline_path: Path) -> dict[str, Any]:
    """Compare a result with the committed regression baseline."""
    baseline_path = Path(baseline_path)
    baseline = _mapping(json.loads(baseline_path.read_text(encoding="utf-8")), "baseline")
    differences: list[str] = []
    for field in ("corpus_version", "evaluator_version", "graph_version"):
        if baseline.get(field) != result.get(field):
            differences.append(
                f"{field}: baseline {baseline.get(field)!r}, current {result.get(field)!r}"
            )
    if baseline.get("result_hash") != result.get("result_hash"):
        differences.append("result_hash differs from the committed baseline")
    expected_summary = _mapping(baseline.get("summary"), "baseline summary")
    for field in (
        "total_entries",
        "valid_cases",
        "rejection_cases",
        "passed",
        "failed",
        "all_passed",
        "checks_total",
        "checks_passed",
    ):
        if expected_summary.get(field) != result["summary"].get(field):
            differences.append(
                f"summary.{field}: baseline {expected_summary.get(field)!r}, "
                f"current {result['summary'].get(field)!r}"
            )
    return {
        "path": str(baseline_path),
        "passed": not differences,
        "expected_result_hash": baseline.get("result_hash"),
        "actual_result_hash": result.get("result_hash"),
        "differences": differences,
    }


def write_result(result: dict[str, Any], output: Path) -> None:
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the deterministic snapshot quality evaluation."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path, help="write the full result manifest")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="compare against a committed regression baseline",
    )
    args = parser.parse_args(argv)

    result = asyncio.run(
        evaluate_snapshot(args.corpus, baseline_path=args.baseline)
    )
    if args.output:
        write_result(result, args.output)
    payload: dict[str, Any] = {
        "summary": result["summary"],
        "result_hash": result["result_hash"],
    }
    if "baseline" in result:
        payload["baseline"] = result["baseline"]
    print(json.dumps(payload, indent=2))
    baseline_passed = result.get("baseline", {}).get("passed", True)
    return 0 if result["summary"]["all_passed"] and baseline_passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
