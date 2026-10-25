"""Fresh-checkout fixture smoke test using the documented application entry point.

This intentionally exercises no database, network provider or credential. It
proves that the packaged graph and fixture gateways can produce a clearly
labeled synthetic draft from a clean process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOPIC = "A practical guide to building a writing habit"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="fieldnote-smoke-") as temporary:
        output = Path(temporary) / "demo.md"
        environment = os.environ.copy()
        environment["PYTHONPATH"] = str(ROOT / "src")
        environment.pop("DATABASE_URL", None)
        environment.pop("MIGRATION_DATABASE_URL", None)
        subprocess.run(
            [
                sys.executable,
                "-m",
                "fieldnote",
                "demo",
                "--topic",
                TOPIC,
                "--output",
                str(output),
            ],
            cwd=ROOT,
            env=environment,
            check=True,
            timeout=120,
        )
        text = output.read_text(encoding="utf-8")
    assert text.startswith(
        "> Synthetic development draft. No live model or web research was used."
    ), "fixture output must disclose its synthetic provenance"
    assert f"# {TOPIC}" in text, "fixture output must contain the requested title"
    assert "[1]" in text, "fixture output must retain a citation marker"
    assert "Synthetic development example" in text
    print("Fixture smoke passed: synthetic draft, disclosure and citation marker present.")


if __name__ == "__main__":
    main()
