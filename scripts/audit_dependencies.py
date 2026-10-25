"""Read public PyPI/OSV metadata for a hash lock without installing or importing packages.

Run before installing the locked public dependencies. This complements, rather than
replaces, maintainer-advisory and recent supply-chain incident review.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import re
import urllib.request
from pathlib import Path


def fetch(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"User-Agent": "Fieldnote-dependency-review/0.1", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=40) as response:
        return json.load(response)


def inspect_package(item: tuple[str, str]) -> dict:
    name, version = item
    info = fetch(f"https://pypi.org/pypi/{name}/{version}/json")
    files = info["urls"]
    return {
        "name": name, "version": version,
        "metadata_url": f"https://pypi.org/pypi/{name}/{version}/json",
        "requires_python": info["info"].get("requires_python"),
        "project_urls": info["info"].get("project_urls"),
        "pypi_vulnerabilities": info.get("vulnerabilities", []),
        "files": [{"filename": f["filename"], "sha256": f["digests"]["sha256"],
                   "yanked": f["yanked"], "uploaded": f["upload_time_iso_8601"]} for f in files],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lock", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    content = args.lock.read_text(encoding="utf-8")
    packages = re.findall(r"^([a-zA-Z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;\\]+)", content, re.M)
    if not packages:
        raise SystemExit("No exact package pins found; refusing empty audit.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        rows = list(executor.map(inspect_package, packages))
    queries = [{"package": {"ecosystem": "PyPI", "name": n}, "version": v} for n, v in packages]
    osv = fetch("https://api.osv.dev/v1/querybatch", {"queries": queries})["results"]
    findings = []
    for row, result in zip(rows, osv, strict=True):
        row["osv"] = result.get("vulns", [])
        if row["pypi_vulnerabilities"] or row["osv"] or all(f["yanked"] for f in row["files"]):
            findings.append({"name": row["name"], "version": row["version"], "osv": row["osv"],
                             "pypi": row["pypi_vulnerabilities"]})
    report = {"checked_at_utc": dt.datetime.now(dt.UTC).isoformat(),
              "lock_sha256": hashlib.sha256(args.lock.read_bytes()).hexdigest(),
              "scope": "Public PyPI registry and OSV metadata; no installation or package execution",
              "packages": rows, "findings": findings,
              "caveat": "No advisory result guarantees safety; review provenance and current maintainer incidents separately."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Reviewed {len(rows)} exact pins; {len(findings)} packages require finding review. Report: {args.output}")
    for finding in findings:
        print(json.dumps(finding))
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
