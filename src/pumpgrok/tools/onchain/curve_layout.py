"""Bonding-curve account decode over Solana RPC (STRUCT-002, B9, T-03-02-01).

Verified header (IDL order), read from a mainnet bonding-curve account owned by
program ``6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P``:

* 8-byte Anchor discriminator ``17b7f83760d8ac60`` (``account:BondingCurve``)
* ``virtual_token_reserves`` u64
* ``virtual_sol_reserves`` u64
* ``real_token_reserves`` u64
* ``real_sol_reserves`` u64
* ``token_total_supply`` u64
* ``complete`` bool (0 or 1)

Live accounts are longer than this 49-byte header (166 bytes observed). The
price reader decodes the header and ignores the trailing extension. The
recorded account, its mint, and the PDA check live in
``tests/fixtures/rpc/bonding_curve/account.json``.

A swapped field order puts a token-scale integer in the SOL slot and fails
the virtual-SOL cap below. RPC only: this module performs no HTTP reads and
references no quote endpoint (R44, R46, R47).
"""

from __future__ import annotations

import base64
import struct
from decimal import Decimal
from typing import Final

from pydantic import BaseModel, ConfigDict

from pumpgrok.core.units import LAMPORTS_PER_SOL, lamports_to_sol, sol_to_lamports
from pumpgrok.tools._common import UNITS_PER_TOKEN

_CURVE_STRUCT = struct.Struct("<8sQQQQQB")
CURVE_ACCOUNT_SIZE = _CURVE_STRUCT.size

#: pump.fun program. Bonding-curve accounts are owned by this program.
PUMP_PROGRAM_ID: Final[str] = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"

#: sha256("account:BondingCurve")[:8], confirmed on the recorded mainnet account.
EXPECTED_DISCRIMINATOR: Final[bytes] = bytes.fromhex("17b7f83760d8ac60")

#: Layout verification state (R65). VERIFIED against the recorded account in
#: tests/fixtures/rpc/bonding_curve/. RPC-derived reserves may price a paper
#: entry. A future mismatch fails closed: decode rejects the account and the
#: adapter writes no fill.
LAYOUT_STATUS: Final[str] = "VERIFIED"

#: Lamports of real SOL at which the curve is considered complete (catalog:
#: 85 SOL, the retired oracle ``src/monitor.py:30``).
COMPLETION_SOL_LAMPORTS = sol_to_lamports(Decimal(85))

#: Virtual SOL above this is not a pump.fun bonding curve. The cap is far above
#: observed reserves (completion is near 85 SOL of real SOL) and below the
#: token-scale integers a swapped field order would place in this slot.
MAX_VIRTUAL_SOL_LAMPORTS: Final[int] = 10_000 * LAMPORTS_PER_SOL


class CurveDecodeError(ValueError):
    """The account bytes do not match the verified curve header."""


class CurveState(BaseModel):
    """Decoded bonding-curve counters. Money is int; ratios stay Decimal."""

    model_config = ConfigDict(frozen=True)

    virtual_sol_reserves_lamports: int
    virtual_token_reserves_units: int
    real_sol_reserves_lamports: int
    real_token_reserves_units: int
    total_supply_units: int
    complete: bool


def decode_curve_account(data: bytes) -> CurveState:
    """Decode the verified header. Trailing bytes (creator and extensions) are ignored.

    Raises :class:`CurveDecodeError` on a short buffer, a wrong discriminator,
    a completion flag outside ``{0, 1}``, a zero price, or reserves that fail
    the SOL-scale cap (the swapped-field trap). Callers map it to ``DataStale``.
    """
    if len(data) < CURVE_ACCOUNT_SIZE:
        raise CurveDecodeError(
            f"curve account size {len(data)} < verified header {CURVE_ACCOUNT_SIZE}"
        )
    header = data[:CURVE_ACCOUNT_SIZE]
    if header[:8] != EXPECTED_DISCRIMINATOR:
        raise CurveDecodeError(
            f"curve discriminator {header[:8].hex()} != {EXPECTED_DISCRIMINATOR.hex()}"
        )
    try:
        (_, vtok, vsol, rtok, rsol, supply, complete) = _CURVE_STRUCT.unpack(header)
    except struct.error as exc:
        raise CurveDecodeError(f"curve account unpack failed: {type(exc).__name__}") from exc
    if complete not in (0, 1):
        raise CurveDecodeError(f"curve completion flag not boolean: {complete!r}")
    if vsol == 0 or vtok == 0 or supply == 0:
        raise CurveDecodeError("curve reserve is zero; price undefined")
    if vsol > MAX_VIRTUAL_SOL_LAMPORTS:
        raise CurveDecodeError("virtual sol reserve exceeds the verified bonding-curve range")
    if rsol > vsol:
        raise CurveDecodeError("real sol reserve exceeds virtual sol")
    if rtok > vtok:
        raise CurveDecodeError("real token reserve exceeds virtual token")
    return CurveState(
        virtual_sol_reserves_lamports=vsol,
        virtual_token_reserves_units=vtok,
        real_sol_reserves_lamports=rsol,
        real_token_reserves_units=rtok,
        total_supply_units=supply,
        complete=bool(complete),
    )


def decode_curve_account_base64(data: object) -> CurveState:
    """Decode the ``data`` field of a base64 ``getAccountInfo`` value.

    Accepts only the ``["<base64>", "base64"]`` pair form. The ``jsonParsed``
    object form carries no curve bytes and is rejected as malformed.
    """
    if not isinstance(data, list) or len(data) != 2 or data[1] != "base64":
        raise CurveDecodeError("curve account is not base64-encoded bytes")
    try:
        raw = base64.b64decode(data[0], validate=True)
    except (ValueError, TypeError) as exc:
        raise CurveDecodeError("curve account base64 is malformed") from exc
    return decode_curve_account(raw)


def encode_curve_fixture(
    *,
    virtual_sol_reserves_lamports: int,
    virtual_token_reserves_units: int,
    real_sol_reserves_lamports: int,
    real_token_reserves_units: int,
    total_supply_units: int,
    complete: bool,
) -> bytes:
    """Build verified-header account bytes for tests. Never used on the wire."""
    return _CURVE_STRUCT.pack(
        EXPECTED_DISCRIMINATOR,
        virtual_token_reserves_units,
        virtual_sol_reserves_lamports,
        real_token_reserves_units,
        real_sol_reserves_lamports,
        total_supply_units,
        1 if complete else 0,
    )


def curve_price_sol_per_token(state: CurveState) -> Decimal:
    """SOL per whole token from virtual reserves.

    ``(vsol / 1e9) / (vtok / 1e6)`` in :class:`Decimal`. ``vtok`` is base units
    (6 decimals). The result is SOL per token, never a float.
    """
    return lamports_to_sol(
        Decimal(state.virtual_sol_reserves_lamports)
        / Decimal(state.virtual_token_reserves_units)
        * Decimal(UNITS_PER_TOKEN)
    )


def curve_progress(state: CurveState) -> Decimal:
    """Completion ratio 0..1 from real SOL against the 85 SOL bar."""
    ratio = Decimal(state.real_sol_reserves_lamports) / Decimal(COMPLETION_SOL_LAMPORTS)
    return min(Decimal(1), max(Decimal(0), ratio))


def curve_market_cap_lamports(state: CurveState) -> int:
    """Market cap in lamports: virtual SOL scaled by supply over virtual tokens."""
    return (
        state.virtual_sol_reserves_lamports * state.total_supply_units
    ) // state.virtual_token_reserves_units


def rpc_curve_price_allowed(*, curve_source: str) -> bool:
    """True when a price from ``curve_source`` may price a paper entry.

    RPC reserves are allowed while ``LAYOUT_STATUS`` is VERIFIED. Any other
    status fail-closes RPC entries with ``data_unavailable``. Fixture, rest
    and pumpportal integer ticks never pass through this layout.
    """
    return not (curve_source == "rpc" and LAYOUT_STATUS != "VERIFIED")
