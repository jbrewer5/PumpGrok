"""Seat rules: cron state verbs still refuse sniper/exit; paper trades do not.

``jobs.yaml`` ``forbidden_owners`` keeps scheduled jobs off the only seats that
may send. Paper buys are sniper-only and paper sells are exit-only, which is
the desk rule. Those seats are refused on state verbs, where the cron list
still applies.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from pumpgrok.cli import EXIT_CONFIG, main
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

TICKET = "SOL-20260101-001"
MINT = "So11111111111111111111111111111111111111112"
APPROVAL = "apr-01ARZ3NDEKTSV4RRFFQ69G5FAV"

TRADE_FLAGS = [
    "--ticket",
    TICKET,
    "--mint",
    MINT,
    "--size-sol",
    "0.5",
    "--slippage-bps",
    "100",
    "--approval-id",
    APPROVAL,
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip PUMPGROK_* and secret-shaped names so each test sets exactly what it needs."""
    for name in list(os.environ):
        if name.startswith("PUMPGROK_") or "KEY" in name.upper():
            monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def root_handlers() -> Iterator[None]:
    """main() installs a handler bound to the test's stderr; remove it afterwards."""
    root = logging.getLogger()
    level = root.level
    yield
    for handler in root.handlers[:]:
        root.removeHandler(handler)
    root.setLevel(level)


def _paper_verb(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, side: str, seat: str) -> int:
    monkeypatch.chdir(tmp_path)
    return main(["paper", side, *TRADE_FLAGS, "--seat", seat])


@pytest.mark.parametrize(
    ("side", "seat"),
    [("buy", "exit"), ("sell", "sniper"), ("buy", "desk"), ("sell", "chief")],
)
def test_wrong_seat_refused_on_paper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    side: str,
    seat: str,
) -> None:
    code = _paper_verb(tmp_path, monkeypatch, side, seat)
    assert code == EXIT_CONFIG
    captured = capsys.readouterr()
    assert "refused" in captured.err
    assert json.loads(captured.out)["code"] == "seat_may_trade"


@pytest.mark.parametrize(
    "verb",
    [["state", "status"], ["state", "halt"], ["state", "autonomy", "manual-ticket"]],
)
@pytest.mark.parametrize("seat", ["sniper", "exit"])
def test_forbidden_seats_refused_on_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: list[str],
    seat: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    # The autonomy verb requires --by at parse time; supply it so parsing
    # succeeds and the forbidden-seat refusal (not argparse) is exercised.
    argv = [*verb, "--seat", seat]
    if verb[:2] == ["state", "autonomy"]:
        argv += ["--by", "human:alice"]
    assert main(argv) == EXIT_CONFIG
    assert "refused" in capsys.readouterr().err


def test_sniper_buy_is_not_a_forbidden_owner_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The short name sniper passes the trade seat gate. No curve is mocked, so the
    read fails closed and nothing is filled. The refusal is not seat_may_trade.
    """

    async def call(self: RpcClient, method: str, params: list[object]) -> object:
        del self, params
        if method == "getAccountInfo":
            return {"context": {"slot": 1}, "value": None}
        if method == "getRecentPrioritizationFees":
            return [{"slot": 1, "prioritizationFee": 1}]
        raise RpcError(f"unexpected method {method}")

    monkeypatch.setattr(RpcClient, "call", call)
    code = _paper_verb(tmp_path, monkeypatch, "buy", "sniper")
    assert code == EXIT_CONFIG
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] != "seat_may_trade"
    assert payload["ok"] is False


def test_bad_ticket_is_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(
        [
            "paper",
            "buy",
            "--ticket",
            "nope",
            "--mint",
            MINT,
            "--size-sol",
            "0.5",
            "--slippage-bps",
            "100",
            "--approval-id",
            APPROVAL,
            "--seat",
            "desk",
        ]
    )
    assert code == EXIT_CONFIG
    assert "bad --ticket" in capsys.readouterr().err
