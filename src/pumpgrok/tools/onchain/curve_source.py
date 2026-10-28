"""RPC bonding-curve reads for the paper adapter (verified header).

Derives the pump.fun bonding-curve PDA from the mint and decodes
``getAccountInfo``. The snapshot's SOL and token figures are the virtual
reserves, which are what the fill math prices. ``source`` is ``rpc`` so the
layout gate can fail closed if verification is ever withdrawn. No signing,
no transaction, no key material.
"""

from __future__ import annotations

from pumpgrok.execution.curve_pda import bonding_curve_pda
from pumpgrok.execution.paper_adapter.models import CurveSnapshot
from pumpgrok.tools.onchain.curve_layout import (
    PUMP_PROGRAM_ID,
    CurveDecodeError,
    decode_curve_account_base64,
)
from pumpgrok.tools.onchain.rpc import RpcClient, RpcError


def _account_value(result: object) -> tuple[dict[str, object] | None, int | None]:
    """The account object and slot, or ``(None, None)`` when the read cannot be priced."""
    if not isinstance(result, dict):
        return None, None
    value = result.get("value")
    if not isinstance(value, dict) or value.get("owner") != PUMP_PROGRAM_ID:
        return None, None
    context = result.get("context")
    slot = context.get("slot") if isinstance(context, dict) else None
    return value, slot if isinstance(slot, int) else None


class RpcCurveSource:
    """PriceSource backed by one injected RPC client."""

    def __init__(self, rpc: RpcClient) -> None:
        self._rpc = rpc

    async def curve(self, mint: str) -> CurveSnapshot | None:
        """Virtual reserves for ``mint``, or None when the account cannot be priced."""
        try:
            account = bonding_curve_pda(mint)
            result = await self._rpc.call("getAccountInfo", [account, {"encoding": "base64"}])
        except (ValueError, RpcError):
            return None
        value, slot = _account_value(result)
        if value is None:
            return None
        try:
            state = decode_curve_account_base64(value.get("data"))
        except CurveDecodeError:
            return None
        return CurveSnapshot(
            mint=mint,
            s_lamports=state.virtual_sol_reserves_lamports,
            t_units=state.virtual_token_reserves_units,
            complete=state.complete,
            source="rpc",
            slot=slot,
        )
