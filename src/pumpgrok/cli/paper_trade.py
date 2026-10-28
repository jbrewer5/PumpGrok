"""Paper buy/sell: quote, simulate, then one place. No retry, no live send.

SNIPER is the only seat that may buy. EXIT is the only seat that may sell.
The fill is priced from the verified bonding-curve header through the paper
adapter. ``venue_tx_ref`` is ``dry_run:<ulid>``. The bare marker ``tx_hash:
dry_run`` is the export flag for pipeline matching and is never reported here
as a fill.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

from pydantic import ValidationError

from pumpgrok.cli.codes import EXIT_CONFIG, EXIT_OK
from pumpgrok.cli.errors import _say, _tool_error
from pumpgrok.core.errors import OrderRejectedError, PumpgrokError
from pumpgrok.core.ids import ULID_RE
from pumpgrok.core.tool import Tool
from pumpgrok.execution.curve_pda import bonding_curve_pda
from pumpgrok.execution.paper_adapter.marks import record_curve_mark
from pumpgrok.ledger.paths import PAPER_LEDGER_PATH
from pumpgrok.ledger.store import LedgerStore
from pumpgrok.replay.clock import WallClock

_UNKNOWN = "UNKNOWN RESULT - reconcile by signature"
_VIRTUAL_UNITS = "virtual_token_reserves_units"  # pragma: allowlist secret: reserve field
_WSOL = "So11111111111111111111111111111111111111112"
_DRY_RUN_REF = r"^dry_run:[0-9A-HJKMNP-TV-Z]{26}$"

# Cron jobs must not be owned by these seats (jobs.yaml). Paper orders are the
# opposite rule: only these seats may send, and only on their own side.
BUY_SEATS = frozenset({"sniper", "bot-04-sniper"})
SELL_SEATS = frozenset({"exit", "bot-06-exit"})


def refuse_trade_seat(seat: str, side: str, verb: str) -> int | None:
    """Refuse every seat except sniper buys and exit sells. None means allowed."""
    allowed = BUY_SEATS if side == "buy" else SELL_SEATS
    if seat in allowed:
        return None
    who = "sniper" if side == "buy" else "exit"
    message = f"seat {seat!r} cannot paper {side}; {side}s are {who} only"
    _say(sys.stdout, json.dumps({"ok": False, "code": "seat_may_trade", "message": message}))
    _say(sys.stderr, f"refused: {message} for `{verb}`")
    return EXIT_CONFIG


def _unknown(verb: str) -> int:
    """Ambiguous place: say so once and stop. Never a second send."""
    _say(sys.stdout, json.dumps({"ok": False, "code": "unknown", "message": _UNKNOWN}))
    _say(sys.stderr, f"{verb}: {_UNKNOWN}")
    return EXIT_CONFIG


def _fill_for(decision_id: str) -> dict[str, Any] | None:
    """The FillRecorded row for this decision, if the ledger has one."""
    if not PAPER_LEDGER_PATH.exists():
        return None
    store = LedgerStore(str(PAPER_LEDGER_PATH))
    try:
        for event in store.read(types=["FillRecorded"], limit=None):
            payload = event.get("payload")
            if isinstance(payload, dict) and payload.get("decision_id") == decision_id:
                return payload
    finally:
        store.close()
    return None


def _public_fill(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A paper fill the ledger recorded. A bare dry_run hash is not enough."""
    ref = payload.get("venue_tx_ref")
    if not isinstance(ref, str) or re.fullmatch(_DRY_RUN_REF, ref) is None:
        return None
    return {
        "venue_tx_ref": ref,
        "filled_lamports": payload.get("filled_lamports"),
        "filled_units": payload.get("filled_units"),
        "side": payload.get("side"),
        "price_avg": payload.get("price_avg"),
    }


def _from_tool_error(verb: str, exc: Exception, *, placed: bool) -> int:
    """Map a tool failure. A timeout after place is UNKNOWN RESULT; earlier it is not."""
    if isinstance(exc, OrderRejectedError):
        return _tool_error(verb, exc.reason, exc.detail or str(exc))
    if isinstance(exc, ValidationError):
        return _tool_error(verb, "invalid_arguments", str(exc.errors()[0]["msg"]))
    if isinstance(exc, PumpgrokError):
        return _tool_error(verb, exc.code, exc.message)
    if isinstance(exc, TimeoutError):
        if placed:
            return _unknown(verb)
        return _tool_error(
            verb, "tool_timeout", "quote or simulate timed out; order was not placed"
        )
    raise exc


async def _preview(
    tools: list[Tool],
    *,
    verb: str,
    side: str,
    ticket: str,
    mint: str,
    curve_account: str,
    size_lamports: int,
    slippage_bps: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]] | int:
    """Price, fee, quote, and simulate. An int return means do not place."""
    price_tool, fee_tool, quote_tool, simulate_tool = tools
    try:
        price_out = await price_tool(
            price_tool.spec.input_model.model_validate(
                {"mint": mint, "curve_account": curve_account}
            )
        )
        await fee_tool(fee_tool.spec.input_model.model_validate({}))
        quote_out = await quote_tool(
            quote_tool.spec.input_model.model_validate(
                {
                    "input_mint": _WSOL,
                    "output_mint": mint,
                    "amount": size_lamports,
                    "slippage_bps": slippage_bps,
                }
            )
        )
        sim_out = await simulate_tool(
            simulate_tool.spec.input_model.model_validate(
                {
                    "ticket_id": ticket,
                    "mint": mint,
                    "side": side,
                    "size_lamports": size_lamports,
                    "slippage_bps": slippage_bps,
                }
            )
        )
    except (OrderRejectedError, ValidationError, PumpgrokError, TimeoutError) as exc:
        return _from_tool_error(verb, exc, placed=False)
    price = price_out.model_dump(mode="json")
    simulation = sim_out.model_dump(mode="json")
    if not simulation.get("would_fill"):
        reason = str(simulation.get("rejection_reason") or "simulation_rejected")
        return _tool_error(verb, reason, "simulation rejected the order; nothing was placed")
    return price, quote_out.model_dump(mode="json"), simulation


async def run_paper_trade(
    tools: list[Tool],
    *,
    verb: str,
    side: str,
    ticket: str,
    mint: str,
    size_lamports: int,
    slippage_bps: int,
    approval_id: str,
    decision_id: str,
) -> int:
    """Quote, simulate, then place once. A failed earlier step does not place."""
    if re.fullmatch(ULID_RE, decision_id) is None:
        return _tool_error(verb, "invalid_arguments", "decision id is not a ULID")
    try:
        curve_account = bonding_curve_pda(mint)
    except ValueError as exc:
        return _tool_error(verb, "invalid_arguments", str(exc))

    preview = await _preview(
        tools[:4],
        verb=verb,
        side=side,
        ticket=ticket,
        mint=mint,
        curve_account=curve_account,
        size_lamports=size_lamports,
        slippage_bps=slippage_bps,
    )
    if isinstance(preview, int):
        return preview
    price, quote, simulation = preview
    if side == "sell" and price.get("graduated") is not True:
        mark_store = LedgerStore(str(PAPER_LEDGER_PATH))
        try:
            record_curve_mark(
                mark_store,
                WallClock(),
                mint=mint,
                s_lamports=int(price.get("virtual_sol_reserves_lamports") or 0),
                t_units=int(price.get(_VIRTUAL_UNITS) or 0),
            )
        finally:
            mark_store.close()
    place_tool = tools[4]
    try:
        placed = await place_tool(
            place_tool.spec.input_model.model_validate(
                {
                    "ticket_id": ticket,
                    "mint": mint,
                    "side": side,
                    "decision_id": decision_id,
                    "size_lamports": size_lamports,
                    "max_slippage_bps": slippage_bps,
                    "approval_id": approval_id,
                }
            )
        )
    except (ValidationError, PumpgrokError, TimeoutError) as exc:
        return _from_tool_error(verb, exc, placed=True)

    return _report_place(verb, decision_id, quote, simulation, placed.model_dump(mode="json"))


def _report_place(
    verb: str,
    decision_id: str,
    quote: dict[str, Any],
    simulation: dict[str, Any],
    place: dict[str, Any],
) -> int:
    """Print one fill, or the refusal. Unknown means stop; there is no second send."""
    status = str(place.get("status") or "")
    if status == "unknown":
        return _unknown(verb)
    if status not in ("filled", "partial"):
        reason = str(place.get("reason") or "rejected")
        return _tool_error(verb, reason, f"paper order {status or 'rejected'}")
    recorded = _fill_for(decision_id)
    public = _public_fill(recorded) if recorded is not None else None
    if public is None:
        return _unknown(verb)
    _say(
        sys.stdout,
        json.dumps(
            {
                "ok": True,
                "status": status,
                "decision_id": decision_id,
                "quote": quote,
                "simulation": simulation,
                "place": place,
                "fill": public,
                "chain_submitted": False,
            }
        ),
    )
    return EXIT_OK
