"""PumpSwap pool header from the official pump_amm IDL.

Program ``pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA``. The header is the
IDL ``Pool`` account: discriminator, bump, index, creator, base and quote
mints, the two vaults, then the appended fee fields. Shorter accounts (written
before a field existed) read the missing tail as zero.

Spot price uses the vaults' token balances. The quote side is the effective
reserve ``vault_amount + virtual_quote_reserves`` (signed), which is the
official quoting rule: raw quote vaults include fees waiting to be swept.
The base side is the raw base vault balance.

Fee schedule below is the decoded mainnet ``GlobalConfig``
(``ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw``): LP 20 bps, protocol 5,
coin-creator 5. A pool ``creator_fee_bps`` other than zero replaces the
standard coin-creator leg (custom quote pairs).
"""

from __future__ import annotations

import struct
from typing import Final

from pumpgrok.execution.curve_pda import b58encode
from pumpgrok.tools.onchain.curve_layout import PUMP_PROGRAM_ID

PUMP_AMM_PROGRAM_ID: Final[str] = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
POOL_DISCRIMINATOR: Final[bytes] = bytes([241, 154, 109, 4, 17, 177, 109, 188])
GLOBAL_CONFIG_DISCRIMINATOR: Final[bytes] = bytes([149, 8, 156, 202, 160, 252, 176, 217])
GLOBAL_CONFIG: Final[str] = "ADyA8hdefvWN2dbGGWFotbzWxrAvLW83WG6QCVXvJKqw"
# Verified WSOL-base / USDC-quote pool. Conversion reads its vaults; it is not a rate.
SOL_USDC_POOL: Final[str] = "53W23c9mtDXgnhqpHJiRmYSKwpRf5mtwHeJM83FDxWFm"

WSOL_MINT: Final[str] = "So11111111111111111111111111111111111111112"
USDC_MINT: Final[str] = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_DECIMALS: Final[int] = 9
USDC_DECIMALS: Final[int] = 6
#: All-zero pubkey. Bonding curves paired with SOL store this as quote_mint.
DEFAULT_PUBKEY: Final[str] = "11111111111111111111111111111111"

# Canonical index for pools created by the pump.fun migrate instruction.
CANONICAL_POOL_INDEX: Final[int] = 0

LP_FEE_BPS: Final[int] = 20
PROTOCOL_FEE_BPS: Final[int] = 5
COIN_CREATOR_FEE_BPS: Final[int] = 5

_HEAD = 8 + 1 + 2 + 32 * 6  # through the quote vault pubkey
_VIRTUAL_OFF = _HEAD + 8 + 32 + 2  # lp supply, coin creator, two bools
_CREATOR_FEE_OFF = _VIRTUAL_OFF + 16


class PumpSwapDecodeError(ValueError):
    """The account bytes are not a PumpSwap pool or global config header."""


class PoolHeader:
    """The fields the price reader needs. Mints and vaults are base58."""

    __slots__ = (
        "base_mint",
        "base_vault",
        "creator_fee_bps",
        "quote_mint",
        "quote_vault",
        "virtual_quote",
    )

    def __init__(
        self,
        *,
        base_mint: str,
        quote_mint: str,
        base_vault: str,
        quote_vault: str,
        virtual_quote: int,
        creator_fee_bps: int,
    ) -> None:
        self.base_mint = base_mint
        self.quote_mint = quote_mint
        self.base_vault = base_vault
        self.quote_vault = quote_vault
        self.virtual_quote = virtual_quote
        self.creator_fee_bps = creator_fee_bps


def _b58(raw: bytes) -> str:
    return b58encode(raw)


def decode_pool_account(data: bytes) -> PoolHeader:
    """Decode a pool account. Raises when the discriminator or length is wrong."""
    if len(data) < _HEAD or data[:8] != POOL_DISCRIMINATOR:
        raise PumpSwapDecodeError("not a PumpSwap pool header")
    off = 8 + 1 + 2
    base = _b58(data[off + 32 : off + 64])
    quote = _b58(data[off + 64 : off + 96])
    base_vault = _b58(data[off + 128 : off + 160])
    quote_vault = _b58(data[off + 160 : off + 192])
    virtual = 0
    if len(data) >= _VIRTUAL_OFF + 16:
        virtual = int.from_bytes(data[_VIRTUAL_OFF : _VIRTUAL_OFF + 16], "little", signed=True)
    creator_fee = 0
    if len(data) >= _CREATOR_FEE_OFF + 8:
        creator_fee = int.from_bytes(data[_CREATOR_FEE_OFF : _CREATOR_FEE_OFF + 8], "little")
    return PoolHeader(
        base_mint=base,
        quote_mint=quote,
        base_vault=base_vault,
        quote_vault=quote_vault,
        virtual_quote=virtual,
        creator_fee_bps=creator_fee,
    )


def decode_global_fees(data: bytes) -> tuple[int, int, int]:
    """``(lp_bps, protocol_bps, coin_creator_bps)`` from a GlobalConfig account."""
    if len(data) < 8 + 32 + 16 or data[:8] != GLOBAL_CONFIG_DISCRIMINATOR:
        raise PumpSwapDecodeError("not a PumpSwap global config")
    off = 8 + 32
    lp_bps, protocol_bps = struct.unpack_from("<QQ", data, off)
    # disable flag, eight recipient pubkeys, then coin-creator bps.
    creator_off = off + 16 + 1 + 32 * 8
    if len(data) < creator_off + 8:
        raise PumpSwapDecodeError("global config has no coin-creator fee")
    (creator_bps,) = struct.unpack_from("<Q", data, creator_off)
    return int(lp_bps), int(protocol_bps), int(creator_bps)


def fee_bps(lp_bps: int, protocol_bps: int, coin_creator_bps: int, pool_creator_bps: int) -> int:
    """Total venue bps. A non-zero pool creator rate replaces the standard leg."""
    creator = pool_creator_bps if pool_creator_bps > 0 else coin_creator_bps
    return int(lp_bps) + int(protocol_bps) + int(creator)


def token_amount(data: bytes) -> int:
    """SPL token-account balance. Amount is the u64 at offset 64."""
    if len(data) < 72:
        raise PumpSwapDecodeError("token account is shorter than the amount field")
    (amount,) = struct.unpack_from("<Q", data, 64)
    return int(amount)


def mint_decimals(data: bytes) -> int:
    """SPL mint decimals. The byte at offset 44 is the decimals field."""
    if len(data) < 45:
        raise PumpSwapDecodeError("mint account is shorter than the decimals field")
    return int(data[44])


def effective_quote(vault_amount: int, virtual_quote: int) -> int:
    """Official effective quote reserves. Negative or zero is not a price."""
    total = int(vault_amount) + int(virtual_quote)
    if total <= 0:
        raise PumpSwapDecodeError("effective quote reserves are not positive")
    return total


def quote_is_sol(quote_mint: str | None) -> bool:
    """True for wrapped SOL and for the all-zero quote mint on SOL bonding curves."""
    return quote_mint is None or quote_mint in (WSOL_MINT, DEFAULT_PUBKEY)


def curve_quote_mint(data: bytes) -> str | None:
    """Quote mint on a bonding-curve account, or None when the tail is absent.

    None means the legacy SOL curve. The field sits after the creator pubkey
    and the two flag bytes (offset 83).
    """
    if len(data) < 115:
        return None
    return _b58(data[83:115])


# Re-export so callers do not import the pump program id from two places.
__all__ = [
    "CANONICAL_POOL_INDEX",
    "COIN_CREATOR_FEE_BPS",
    "DEFAULT_PUBKEY",
    "GLOBAL_CONFIG",
    "LP_FEE_BPS",
    "PROTOCOL_FEE_BPS",
    "PUMP_AMM_PROGRAM_ID",
    "PUMP_PROGRAM_ID",
    "SOL_USDC_POOL",
    "USDC_DECIMALS",
    "USDC_MINT",
    "WSOL_MINT",
    "PoolHeader",
    "PumpSwapDecodeError",
    "curve_quote_mint",
    "decode_global_fees",
    "decode_pool_account",
    "effective_quote",
    "fee_bps",
    "mint_decimals",
    "quote_is_sol",
    "token_amount",
]
