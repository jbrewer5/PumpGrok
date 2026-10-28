"""PumpSwap price from recorded mainnet accounts.

The pool, its vaults, the WSOL/USDC pool, and GlobalConfig are getAccountInfo
snapshots. Price uses raw base vaults and effective quote reserves. A non-SOL
quote without those bytes is BLIND.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from pumpgrok.execution.curve_pda import bonding_curve_pda, canonical_pool_pda
from pumpgrok.tools.onchain.curve_layout import PUMP_PROGRAM_ID
from pumpgrok.tools.onchain.curve_source import RpcCurveSource
from pumpgrok.tools.onchain.pumpswap_layout import (
    COIN_CREATOR_FEE_BPS,
    LP_FEE_BPS,
    PROTOCOL_FEE_BPS,
    USDC_DECIMALS,
    USDC_MINT,
    WSOL_MINT,
    PumpSwapDecodeError,
    curve_quote_mint,
    decode_global_fees,
    decode_pool_account,
    effective_quote,
    fee_bps,
    mint_decimals,
    quote_is_sol,
    token_amount,
)

_ACCOUNTS = json.loads(
    Path("tests/fixtures/rpc/pumpswap/accounts.json").read_text(encoding="utf-8")
)
_HYPE = json.loads(
    Path("tests/fixtures/rpc/bonding_curve/account.json").read_text(encoding="utf-8")
)
_BASE = "CZv9RdLNSpEDcAd4kDRSZKPkSgPnGsG4ZV64e8iqpump"


def _raw(name: str) -> bytes:
    row = _ACCOUNTS[name]
    raw = base64.b64decode(row["b64"], validate=True)
    assert len(raw) == row["len"]
    return raw


def test_recorded_pool_matches_the_idl_and_the_pda() -> None:
    header = decode_pool_account(_raw("pool"))
    assert header.base_mint == _BASE
    assert header.quote_mint == WSOL_MINT
    assert canonical_pool_pda(_BASE, WSOL_MINT) == _ACCOUNTS["pool"]["address"]
    base = token_amount(_raw("base_ta"))
    quote = token_amount(_raw("quote_ta"))
    effective = effective_quote(quote, header.virtual_quote)
    assert base > 0
    assert effective > 0
    assert mint_decimals(_raw("base_mint")) == 6
    assert mint_decimals(_raw("usdc_base_mint")) == 9
    assert mint_decimals(_raw("usdc_quote_mint")) == USDC_DECIMALS


def test_effective_quote_adds_the_signed_virtual_reserve() -> None:
    header = decode_pool_account(_raw("pool"))
    vault = token_amount(_raw("quote_ta"))
    assert effective_quote(vault, header.virtual_quote) == vault + header.virtual_quote
    with pytest.raises(PumpSwapDecodeError):
        effective_quote(1, -5)


def test_which_side_is_the_token() -> None:
    sol_pool = decode_pool_account(_raw("pool"))
    assert sol_pool.base_mint == _BASE
    assert sol_pool.quote_mint == WSOL_MINT
    usdc_pool = decode_pool_account(_raw("usdc_pool"))
    assert usdc_pool.base_mint == WSOL_MINT
    assert usdc_pool.quote_mint == USDC_MINT
    assert not quote_is_sol(USDC_MINT)
    assert quote_is_sol(WSOL_MINT)
    assert quote_is_sol("11111111111111111111111111111111")


def test_usdc_converts_with_integer_vault_ratio() -> None:
    header = decode_pool_account(_raw("usdc_pool"))
    wsol = token_amount(_raw("usdc_base_ta"))
    usdc = effective_quote(token_amount(_raw("usdc_quote_ta")), header.virtual_quote)
    one_usdc = 1_000_000
    lamports = one_usdc * wsol // usdc
    assert wsol > 0 and usdc > 0
    assert lamports > 0
    assert one_usdc * wsol // usdc == lamports


def test_fee_schedule_comes_from_the_global_config() -> None:
    lp_bps, protocol_bps, creator_bps = decode_global_fees(_raw("global"))
    assert (lp_bps, protocol_bps, creator_bps) == (
        LP_FEE_BPS,
        PROTOCOL_FEE_BPS,
        COIN_CREATOR_FEE_BPS,
    )
    assert fee_bps(lp_bps, protocol_bps, creator_bps, 0) == 30
    assert fee_bps(lp_bps, protocol_bps, creator_bps, 40) == 20 + 5 + 40


def test_hype_quote_curve_is_not_sol() -> None:
    raw = base64.b64decode(_HYPE["account_b64"], validate=True)
    quote = curve_quote_mint(raw)
    assert quote is not None
    assert not quote_is_sol(quote)


class _MapRpc:
    def __init__(self, rows: dict[str, dict[str, object]]) -> None:
        self._rows = rows

    async def call(self, method: str, params: list[object]) -> dict[str, object]:
        assert method == "getAccountInfo"
        row = self._rows.get(str(params[0]))
        if row is None:
            return {"context": {"slot": 1}, "value": None}
        return {
            "context": {"slot": 1},
            "value": {"owner": row["owner"], "data": [row["b64"], "base64"]},
        }


def _by_address(*names: str) -> dict[str, dict[str, object]]:
    return {str(_ACCOUNTS[name]["address"]): _ACCOUNTS[name] for name in names}


async def test_missing_curve_prices_the_canonical_wsol_pool() -> None:
    source = RpcCurveSource(_MapRpc(_by_address("pool", "base_ta", "quote_ta", "global")))  # type: ignore[arg-type]
    snapshot = await source.curve(_BASE)
    assert snapshot is not None
    assert snapshot.blind is False
    assert snapshot.venue == "pumpswap"
    assert snapshot.fee_bps == 30
    assert snapshot.t_units == token_amount(_raw("base_ta"))
    header = decode_pool_account(_raw("pool"))
    quote = effective_quote(token_amount(_raw("quote_ta")), header.virtual_quote)
    assert snapshot.s_lamports == quote


async def test_non_sol_quote_without_a_pool_is_blind() -> None:
    mint = str(_HYPE["mint"])
    rows = {bonding_curve_pda(mint): {"owner": PUMP_PROGRAM_ID, "b64": _HYPE["account_b64"]}}
    source = RpcCurveSource(_MapRpc(rows))  # type: ignore[arg-type]
    snapshot = await source.curve(mint)
    assert snapshot is not None
    assert snapshot.blind is True


def test_short_or_foreign_account_is_rejected() -> None:
    with pytest.raises(PumpSwapDecodeError):
        decode_pool_account(b"\x00" * 40)
    raw = bytearray(_raw("pool"))
    raw[0] = 0
    with pytest.raises(PumpSwapDecodeError):
        decode_pool_account(bytes(raw))
