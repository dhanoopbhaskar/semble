from __future__ import annotations

import json
from pathlib import Path

from semble_graph.cli import main


def _payload(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def test_cli_build_status_symbol_overview(sample_repo: Path, tmp_path: Path, capsys) -> None:
    cache = tmp_path / "cache"
    assert main(["build", str(sample_repo), "--cache-dir", str(cache)]) == 0
    built = _payload(capsys)
    assert built["stats"]["symbols"] > 0

    assert main(["status", str(sample_repo), "--cache-dir", str(cache)]) == 0
    status = _payload(capsys)
    assert status["status"] == "fresh"

    assert main(["symbol", "PaymentService", "--repo", str(sample_repo), "--cache-dir", str(cache)]) == 0
    symbol = _payload(capsys)
    assert symbol["matches"][0]["qualified_name"].endswith("PaymentService")

    assert main(["overview", str(sample_repo), "--cache-dir", str(cache)]) == 0
    overview = _payload(capsys)
    assert overview["core_symbols"]

    assert main(["doctor", "--cache-dir", str(cache)]) == 0
    doctor = _payload(capsys)
    assert "semble" in doctor


def test_cli_text_output(sample_repo: Path, tmp_path: Path, capsys) -> None:
    cache = tmp_path / "cache"
    assert main(["build", str(sample_repo), "--cache-dir", str(cache)]) == 0
    capsys.readouterr()
    assert (
        main(
            [
                "symbol",
                "PaymentService",
                "--repo",
                str(sample_repo),
                "--cache-dir",
                str(cache),
                "--format",
                "text",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "PaymentService" in out
