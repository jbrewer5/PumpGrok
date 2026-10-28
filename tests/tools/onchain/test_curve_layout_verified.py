"""Verified bonding-curve header against the recorded mainnet account.

The account bytes are a getAccountInfo snapshot. Field order is the pump.fun
IDL order. A swap of the first two counters fails the virtual-SOL cap.
Replay ticks stay integer reserves and do not pass through this decoder.
"""

from __future__ import annotations

import base64
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from pumpgrok.execution.curve_pda import bonding_curve_pda
from pumpgrok.replay.clock import FixtureClock
from pumpgrok.replay.fixtures import event_epoch, load_dataset
from pumpgrok.replay.sources import FixtureCurveSource
from pumpgrok.tools.onchain.curve_layout import (
    LAYOUT_STATUS,
    CurveDecodeError,
    curve_price_sol_per_token,
    decode_curve_account,
)

_ACCOUNT = Path("tests/fixtures/rpc/bonding_curve/account.json")
_SMOKE = Path("tests/fixtures/replay/smoke")


def _record() -> dict:
    return json.loads(_ACCOUNT.read_text(encoding="utf-8"))


def _raw() -> bytes:
    record = _record()
    raw = base64.b64decode(record["account_b64"], validate=True)
    assert hashlib.sha256(raw).hexdigest() == record["account_sha256"]
    assert len(raw) == record["account_len"] == 166
    return raw


def test_recorded_account_decodes_idl_order() -> None:
    record = _record()
    state = decode_curve_account(_raw())
    decoded = record["decoded"]
    assert LAYOUT_STATUS == "VERIFIED"
    assert state.virtual_token_reserves_units == decoded["vtok"]
    assert state.virtual_sol_reserves_lamports == decoded["vsol"]
    assert state.real_token_reserves_units == decoded["rtok"]
    assert state.real_sol_reserves_lamports == decoded["rsol"]
    assert state.total_supply_units == decoded["supply"]
    assert state.complete is False
    gap = record["cross_check"]
    assert (
        state.virtual_token_reserves_units - state.real_token_reserves_units
        == gap["vtok_minus_rtok"]
        == 279_900_000_000_000
    )
    assert (
        state.virtual_sol_reserves_lamports - state.real_sol_reserves_lamports
        == gap["vsol_minus_rsol"]
    )
    assert isinstance(state.virtual_sol_reserves_lamports, int)
    assert state.virtual_sol_reserves_lamports < 10_000 * 1_000_000_000
    price = curve_price_sol_per_token(state)
    assert isinstance(price, Decimal)
    assert price > 0


def test_swapped_sol_and_token_counters_are_rejected() -> None:
    raw = bytearray(_raw())
    raw[8:16], raw[16:24] = raw[16:24], raw[8:16]
    with pytest.raises(CurveDecodeError, match="virtual sol"):
        decode_curve_account(bytes(raw))


def test_recorded_mint_derives_the_recorded_curve() -> None:
    record = _record()
    assert bonding_curve_pda(record["mint"]) == record["account"]


async def test_replay_smoke_prices_integer_ticks() -> None:
    dataset = load_dataset(_SMOKE)
    first = next(event for event in dataset.events if isinstance(event.get("S"), int))
    clock = FixtureClock(0.0)
    source = FixtureCurveSource(dataset, clock)
    clock.advance_to(event_epoch(first))
    snapshot = await source.curve(str(first["mint"]))
    assert snapshot is not None
    assert snapshot.source == "fixture"
    assert snapshot.s_lamports == int(first["S"])
    assert snapshot.t_units == int(first["T"])
    assert isinstance(snapshot.s_lamports, int)
    assert isinstance(snapshot.t_units, int)
