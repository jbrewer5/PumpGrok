"""CLI paper buy/sell records one simulated fill and refuses every other seat.

The curve bytes are the recorded mainnet account. RPC is mocked. No live
transaction is built. A second call with the same approval does not send again.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pumpgrok.cli import EXIT_CONFIG, EXIT_OK, main
from pumpgrok.cli.paper_trade import _UNKNOWN
from pumpgrok.core.approvals import parse_human_phrase, record_human_approval
from pumpgrok.core.idempotency import exec_place_key
from pumpgrok.core.ids import mint_approval_id
from pumpgrok.core.risk_budget import RiskBudget
from pumpgrok.ledger.store import LedgerStore
from pumpgrok.ledger.tickets import allocate_ticket
from pumpgrok.replay.clock import WallClock, format_ts_utc
from pumpgrok.tools.onchain.curve_layout import PUMP_PROGRAM_ID
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

_ACCOUNT = json.loads(
    Path("tests/fixtures/rpc/bonding_curve/account.json").read_text(encoding="utf-8")
)
_SOL_CURVE = json.loads(
    Path("tests/fixtures/rpc/bonding_curve/sol_curve.json").read_text(encoding="utf-8")
)
_MINT = str(_ACCOUNT["mint"])
_WSOL = "So11111111111111111111111111111111111111112"
# SOL-quoted curve (quote mint is the zero pubkey). The other recorded curve is HYPE.
_B64 = str(_SOL_CURVE["b64"])


def _install_rpc(monkeypatch: pytest.MonkeyPatch, *, present: bool) -> None:
    async def call(self: RpcClient, method: str, params: list[object]) -> object:
        del self, params
        if method == "getAccountInfo":
            value = None
            if present:
                value = {
                    "data": [_B64, "base64"],
                    "executable": False,
                    "lamports": 1,
                    "owner": PUMP_PROGRAM_ID,
                    "rentEpoch": 1,
                }
            return {"context": {"slot": 1}, "value": value}
        if method == "getRecentPrioritizationFees":
            return [{"slot": 1, "prioritizationFee": 1000}]
        raise RpcError(f"unexpected method {method}")

    monkeypatch.setattr(RpcClient, "call", call)


def _events(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "var" / "ledger.sqlite"
    if not path.is_file():
        return []
    store = LedgerStore(str(path))
    try:
        return list(store.read(limit=None))
    finally:
        store.close()


def _approve(
    tmp_path: Path, *, side: str, size_sol: str, kind: str, mint: str = _MINT
) -> tuple[str, str]:
    clock = WallClock()
    ts_utc = format_ts_utc(clock.now())
    store = LedgerStore(str(tmp_path / "var" / "ledger.sqlite"))
    try:
        if store.get_meta("initial_equity_lamports") is None and store.count() == 0:
            store.set_meta("initial_equity_lamports", str(RiskBudget().paper_equity_lamports))
        ticket_id = allocate_ticket(
            store,
            ts_utc=ts_utc,
            source="paper",
            correlation_id="t",
            seat="desk",
            title="lead",
        )
        approval_id = mint_approval_id(clock)
        record = parse_human_phrase(
            f"approve {ticket_id} {mint} {size_sol} 100",
            kind=kind,  # type: ignore[arg-type]
            by="human:alice",
            approval_id=approval_id,
            at=datetime.fromtimestamp(clock.now(), UTC),
        )
        record_human_approval(store, record, ts_utc=ts_utc, source="paper", correlation_id="t")
    finally:
        store.close()
    return ticket_id, approval_id


def _trade(side: str, seat: str, ticket: str, approval: str, size: str = "0.05") -> list[str]:
    return [
        "paper",
        side,
        "--ticket",
        ticket,
        "--mint",
        _MINT,
        "--size-sol",
        size,
        "--slippage-bps",
        "100",
        "--approval-id",
        approval,
        "--seat",
        seat,
    ]


def test_sniper_buy_records_one_paper_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _install_rpc(monkeypatch, present=True)
    ticket, approval = _approve(tmp_path, side="buy", size_sol="0.05", kind="human_buy")
    code = main(_trade("buy", "sniper", ticket, approval))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    payload = json.loads(captured.out)
    assert payload["ok"] is True
    assert payload["status"] in ("filled", "partial")
    assert payload["chain_submitted"] is False
    assert "tx_hash" not in payload
    fill = payload["fill"]
    assert str(fill["venue_tx_ref"]).startswith("dry_run:")
    assert fill["venue_tx_ref"] != "dry_run"
    assert int(fill["filled_lamports"]) > 0
    fills = [event for event in _events(tmp_path) if event["type"] == "FillRecorded"]
    assert len(fills) == 1
    assert fills[0]["payload"]["venue_tx_ref"] == fill["venue_tx_ref"]

    again = main(_trade("buy", "bot-04-sniper", ticket, approval))
    assert again == EXIT_CONFIG
    fills_after = [event for event in _events(tmp_path) if event["type"] == "FillRecorded"]
    assert len(fills_after) == 1


def test_exit_sell_records_a_paper_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _install_rpc(monkeypatch, present=True)
    buy_ticket, buy_approval = _approve(tmp_path, side="buy", size_sol="0.05", kind="human_buy")
    assert main(_trade("buy", "sniper", buy_ticket, buy_approval)) == EXIT_OK
    capsys.readouterr()
    sell_ticket, sell_approval = _approve(tmp_path, side="sell", size_sol="0.01", kind="human_sell")
    code = main(_trade("sell", "exit", sell_ticket, sell_approval, size="0.01"))
    captured = capsys.readouterr()
    assert code == EXIT_OK, captured.err
    payload = json.loads(captured.out)
    assert payload["fill"]["side"] == "sell"
    assert str(payload["fill"]["venue_tx_ref"]).startswith("dry_run:")
    sides = [
        event["payload"]["side"] for event in _events(tmp_path) if event["type"] == "FillRecorded"
    ]
    assert sides == ["buy", "sell"]


@pytest.mark.parametrize(
    ("side", "seat"),
    [
        ("buy", "exit"),
        ("buy", "desk"),
        ("buy", "risk"),
        ("sell", "sniper"),
        ("sell", "bot-04-sniper"),
        ("buy", "bot-06-exit"),
    ],
)
def test_other_seats_cannot_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    side: str,
    seat: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    code = main(_trade(side, seat, "SOL-20260101-001", "apr-01ARZ3NDEKTSV4RRFFQ69G5FAV"))
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG
    assert "refused" in captured.err
    payload = json.loads(captured.out)
    assert payload["code"] == "seat_may_trade"
    assert _events(tmp_path) == []


def test_kill_switch_blocks_the_buy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _install_rpc(monkeypatch, present=True)
    latch = tmp_path / "var" / "desk"
    latch.mkdir(parents=True)
    (latch / "KILLSWITCH").write_text("operator halt\n", encoding="utf-8")
    ticket, approval = _approve(tmp_path, side="buy", size_sol="0.05", kind="human_buy")
    code = main(_trade("buy", "sniper", ticket, approval))
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG
    payload = json.loads(captured.out)
    assert payload["code"] == "kill_switch"
    assert not any(event["type"] == "FillRecorded" for event in _events(tmp_path))


def test_max_open_positions_blocks_a_second_mint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PUMPGROK_RISK__MAX_OPEN_POSITIONS", "1")
    _install_rpc(monkeypatch, present=True)
    first_ticket, first_approval = _approve(tmp_path, side="buy", size_sol="0.05", kind="human_buy")
    assert main(_trade("buy", "sniper", first_ticket, first_approval)) == EXIT_OK
    capsys.readouterr()
    second_ticket, second_approval = _approve(
        tmp_path, side="buy", size_sol="0.05", kind="human_buy", mint=_WSOL
    )
    argv = _trade("buy", "sniper", second_ticket, second_approval)
    argv[argv.index("--mint") + 1] = _WSOL
    code = main(argv)
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG, captured.out
    payload = json.loads(captured.out)
    assert payload["code"] == "max_open_positions"
    fills = [event for event in _events(tmp_path) if event["type"] == "FillRecorded"]
    assert len(fills) == 1


def test_unknown_place_is_reported_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    _install_rpc(monkeypatch, present=True)
    calls: list[str] = []

    async def once(
        inp: Any, adapter: object, get_budget: object, clock: object
    ) -> dict[str, object]:
        del adapter, get_budget, clock
        calls.append("place")
        assert len(calls) == 1
        ticket = str(inp.ticket_id)
        order_side = str(inp.side)
        decision = str(inp.decision_id)
        return {
            "ticket_id": ticket,
            "side": order_side,
            "decision_id": decision,
            "idempotency_key": exec_place_key(ticket, order_side, decision),
            "status": "unknown",
            "reason": "confirmation_timeout",
            "meta": {
                "source": "exec",
                "fetched_at": datetime.now(UTC).isoformat(),
                "freshness_s": 0.0,
                "correlation_id": "t",
                "stale": False,
            },
        }

    monkeypatch.setattr("pumpgrok.tools.exec.place.run_place", once)
    ticket, approval = _approve(tmp_path, side="buy", size_sol="0.05", kind="human_buy")
    code = main(_trade("buy", "sniper", ticket, approval))
    captured = capsys.readouterr()
    assert code == EXIT_CONFIG
    assert _UNKNOWN in captured.err
    assert calls == ["place"]
    assert not any(event["type"] == "FillRecorded" for event in _events(tmp_path))
