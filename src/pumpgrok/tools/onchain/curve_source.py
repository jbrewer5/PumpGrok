"""RPC price for the paper adapter: bonding curve, otherwise PumpSwap.

A curve that is still open and quoted in SOL prices from its virtual reserves.
A completed curve prices from the canonical PumpSwap pool (index 0, the migrate
creator). Quote assets other than SOL convert through one verified pool: the
recorded WSOL/USDC pool, or that quote mint's own WSOL pool. Anything else is
``blind`` and must not be priced. No signing and no transaction.
"""

from __future__ import annotations

import base64

from pumpgrok.execution.curve_pda import bonding_curve_pda, canonical_pool_pda
from pumpgrok.execution.paper_adapter.models import CurveSnapshot
from pumpgrok.tools.onchain.curve_layout import (
    PUMP_PROGRAM_ID,
    CurveDecodeError,
    CurveState,
    decode_curve_account,
)
from pumpgrok.tools.onchain.pumpswap_layout import (
    GLOBAL_CONFIG,
    PUMP_AMM_PROGRAM_ID,
    SOL_USDC_POOL,
    USDC_MINT,
    WSOL_MINT,
    PumpSwapDecodeError,
    curve_quote_mint,
    decode_global_fees,
    decode_pool_account,
    effective_quote,
    fee_bps,
    quote_is_sol,
    token_amount,
)
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError

_TOKEN_OWNERS = frozenset(
    {
        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
    }
)


class RpcCurveSource:
    """PriceSource backed by one injected RPC client."""

    def __init__(self, rpc: RpcClient) -> None:
        self._rpc = rpc
        self._schedule: tuple[int, int, int] | None = None

    async def curve(self, mint: str) -> CurveSnapshot | None:  # noqa: PLR0911
        """SOL-denominated reserves for ``mint``, or a blind snapshot, or None."""
        try:
            curve_raw = await self._owned(bonding_curve_pda(mint), PUMP_PROGRAM_ID)
        except (ValueError, RpcError):
            return None
        if curve_raw is None:
            return await self._from_pool(mint, WSOL_MINT)
        try:
            state = decode_curve_account(curve_raw)
        except CurveDecodeError:
            return None
        quote = curve_quote_mint(curve_raw)
        if not state.complete:
            return await self._open_curve(mint, state, quote)
        pooled = await self._from_pool(mint, WSOL_MINT if quote_is_sol(quote) else str(quote))
        if pooled is not None:
            return pooled
        if quote_is_sol(quote):
            return _curve_snapshot(
                mint, state.virtual_sol_reserves_lamports, state.virtual_token_reserves_units, True
            )
        return _blind(mint)

    async def _open_curve(self, mint: str, state: CurveState, quote: str | None) -> CurveSnapshot:
        if quote_is_sol(quote):
            return _curve_snapshot(
                mint, state.virtual_sol_reserves_lamports, state.virtual_token_reserves_units, False
            )
        lamports = await self._to_sol(str(quote), state.virtual_sol_reserves_lamports)
        if lamports is None:
            return _blind(mint)
        return _curve_snapshot(mint, lamports, state.virtual_token_reserves_units, False)

    async def _from_pool(self, token_mint: str, quote_mint: str) -> CurveSnapshot | None:  # noqa: PLR0911
        try:
            pool = canonical_pool_pda(token_mint, quote_mint)
            raw = await self._owned(pool, PUMP_AMM_PROGRAM_ID)
        except (ValueError, RpcError):
            return None
        if raw is None:
            return None
        try:
            header = decode_pool_account(raw)
            base_amt = token_amount(await self._token(header.base_vault))
            quote_amt = token_amount(await self._token(header.quote_vault))
            other_mint, token_raw, other_raw = _sides(
                header.base_mint,
                header.quote_mint,
                token_mint,
                base_amt,
                quote_amt,
                header.virtual_quote,
            )
        except (PumpSwapDecodeError, RpcError, ValueError):
            return None
        if other_mint is None:
            return None
        lamports = await self._to_sol(other_mint, other_raw)
        if lamports is None:
            return _blind(token_mint)
        schedule = await self._fees()
        if schedule is None:
            return _blind(token_mint)
        bps = fee_bps(*schedule, header.creator_fee_bps)
        return CurveSnapshot(
            mint=token_mint,
            s_lamports=lamports,
            t_units=token_raw,
            complete=False,
            source="rpc",
            fee_bps=bps,
            venue="pumpswap",
        )

    async def _to_sol(self, quote_mint: str, amount: int) -> int | None:
        """Quote-token raw units as SOL lamports. None when no verified path exists."""
        if amount <= 0:
            return None
        if quote_is_sol(quote_mint):
            return amount
        if quote_mint == USDC_MINT:
            return await self._via_recorded_usdc(amount)
        return await self._via_wsol_pool(quote_mint, amount)

    async def _via_recorded_usdc(self, usdc_amount: int) -> int | None:
        try:
            raw = await self._owned(SOL_USDC_POOL, PUMP_AMM_PROGRAM_ID)
            if raw is None:
                return None
            header = decode_pool_account(raw)
            if header.base_mint != WSOL_MINT or header.quote_mint != USDC_MINT:
                return None
            wsol = token_amount(await self._token(header.base_vault))
            usdc = effective_quote(
                token_amount(await self._token(header.quote_vault)), header.virtual_quote
            )
        except (PumpSwapDecodeError, RpcError, ValueError):
            return None
        if wsol <= 0 or usdc <= 0:
            return None
        return usdc_amount * wsol // usdc

    async def _via_wsol_pool(self, quote_mint: str, amount: int) -> int | None:
        """One hop: the quote mint's own canonical WSOL pool. No further chaining."""
        try:
            raw = await self._owned(canonical_pool_pda(quote_mint, WSOL_MINT), PUMP_AMM_PROGRAM_ID)
        except (ValueError, RpcError):
            return None
        if raw is None:
            return None
        try:
            header = decode_pool_account(raw)
            if header.base_mint != quote_mint or not quote_is_sol(header.quote_mint):
                return None
            base = token_amount(await self._token(header.base_vault))
            sol = effective_quote(
                token_amount(await self._token(header.quote_vault)), header.virtual_quote
            )
        except (PumpSwapDecodeError, RpcError, ValueError):
            return None
        if base <= 0 or sol <= 0:
            return None
        return amount * sol // base

    async def _fees(self) -> tuple[int, int, int] | None:
        if self._schedule is not None:
            return self._schedule
        try:
            raw = await self._owned(GLOBAL_CONFIG, PUMP_AMM_PROGRAM_ID)
        except RpcError:
            return None
        if raw is None:
            return None
        try:
            self._schedule = decode_global_fees(raw)
        except PumpSwapDecodeError:
            return None
        return self._schedule

    async def _owned(self, address: str, owner: str) -> bytes | None:
        found = await self._load(address)
        if found is None or found[0] != owner:
            return None
        return found[1]

    async def _token(self, address: str) -> bytes:
        found = await self._load(address)
        if found is None or found[0] not in _TOKEN_OWNERS:
            raise PumpSwapDecodeError("token account missing")
        return found[1]

    async def _load(self, address: str) -> tuple[str, bytes] | None:
        result = await self._rpc.call("getAccountInfo", [address, {"encoding": "base64"}])
        if not isinstance(result, dict):
            return None
        value = result.get("value")
        if not isinstance(value, dict):
            return None
        owner = value.get("owner")
        data = value.get("data")
        if not isinstance(owner, str) or not isinstance(data, list) or len(data) != 2:
            return None
        try:
            raw = base64.b64decode(data[0], validate=True)
        except (ValueError, TypeError):
            return None
        return owner, raw


def _sides(  # noqa: PLR0917
    base_mint: str,
    quote_mint: str,
    token_mint: str,
    base_amt: int,
    quote_amt: int,
    virtual_quote: int,
) -> tuple[str | None, int, int]:
    """``(other_mint, token_raw, other_raw)``. The quote side uses the effective reserve."""
    quote_effective = effective_quote(quote_amt, virtual_quote)
    if base_mint == token_mint:
        return quote_mint, base_amt, quote_effective
    if quote_mint == token_mint:
        return base_mint, quote_effective, base_amt
    return None, 0, 0


def _curve_snapshot(mint: str, sol: int, units: int, complete: bool) -> CurveSnapshot:
    return CurveSnapshot(
        mint=mint,
        s_lamports=sol,
        t_units=units,
        complete=complete,
        source="rpc",
        venue="curve",
    )


def _blind(mint: str) -> CurveSnapshot:
    return CurveSnapshot(
        mint=mint,
        s_lamports=1,
        t_units=1,
        complete=False,
        source="blind",
        blind=True,
        venue="blind",
    )
