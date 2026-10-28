"""Paper trades are priced from the verified curve header.

An approved sniper buy and exit sell run through the paper CLI. Coverage of
the fill, the single send, and the seat gate lives in
``tests/cli/test_paper_fills.py``. This module keeps the help text honest:
buys name sniper and sells name exit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pumpgrok.cli import main


def test_paper_help_names_the_trading_seats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as excinfo:
        main(["paper", "buy", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "sniper only" in out
    assert "exit only" in out
    assert "forbidden_owners" not in out
