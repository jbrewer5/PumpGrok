"""Bonding-curve PDA from a mint (pump.fun program, seed ``bonding-curve``).

Pure functions: base58 and the ed25519 on-curve test used by Solana's
``find_program_address``. No RPC, no keys, no signing. The on-curve test
matches curve25519-dalek decompression (a point on the curve is rejected as
a PDA, including small-order points libsodium's valid-point check drops).
"""

from __future__ import annotations

import hashlib

from pumpgrok.tools.onchain.curve_layout import PUMP_PROGRAM_ID

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(_ALPHABET)}
_PDA_MARKER = b"ProgramDerivedAddress"
_CURVE_SEED = b"bonding-curve"

# ed25519 field prime and curve constant d = -121665/121666.
_P = 2**255 - 19
_D = (-121665 * pow(121666, _P - 2, _P)) % _P


def b58decode(text: str) -> bytes:
    """Decode a base58 string. Leading ``1`` characters are zero bytes."""
    if not text or any(char not in _INDEX for char in text):
        raise ValueError("bad base58")
    number = 0
    for char in text:
        number = number * 58 + _INDEX[char]
    pad = 0
    for char in text:
        if char != "1":
            break
        pad += 1
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    return b"\x00" * pad + body


def b58encode(data: bytes) -> str:
    """Encode bytes as base58. Leading zero bytes become ``1``."""
    number = int.from_bytes(data, "big")
    chars: list[str] = []
    while number:
        number, rem = divmod(number, 58)
        chars.append(_ALPHABET[rem])
    pad = 0
    for byte in data:
        if byte != 0:
            break
        pad += 1
    encoded = "1" * pad + "".join(reversed(chars))
    return encoded or "1"


def _on_curve(point: bytes) -> bool:
    """True when ``point`` decompresses as an ed25519 point (Solana PDA rule)."""
    if len(point) != 32:
        return False
    y_coord = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y_coord >= _P:
        return False
    y2 = y_coord * y_coord % _P
    u_num = (y2 - 1) % _P
    v_den = (_D * y2 + 1) % _P
    x2 = (u_num * pow(v_den, _P - 2, _P)) % _P
    if x2 == 0:
        return True
    x_coord = pow(x2, (_P + 3) // 8, _P)
    if (x_coord * x_coord - x2) % _P != 0:
        x_coord = (x_coord * pow(2, (_P - 1) // 4, _P)) % _P
    return (x_coord * x_coord - x2) % _P == 0


def find_program_address(seeds: list[bytes], program_id: bytes) -> tuple[bytes, int]:
    """Solana ``find_program_address``. Returns ``(address, bump)``."""
    if len(program_id) != 32:
        raise ValueError("program id must be 32 bytes")
    for bump in range(255, -1, -1):
        digest = hashlib.sha256(b"".join([*seeds, bytes([bump]), program_id, _PDA_MARKER])).digest()
        if not _on_curve(digest):
            return digest, bump
    raise ValueError("no program address for these seeds")


def bonding_curve_pda(mint: str) -> str:
    """Base58 bonding-curve address for ``mint`` under the pump.fun program."""
    mint_bytes = b58decode(mint)
    program = b58decode(PUMP_PROGRAM_ID)
    if len(mint_bytes) != 32 or len(program) != 32:
        raise ValueError("mint and program must decode to 32 bytes")
    address, _bump = find_program_address([_CURVE_SEED, mint_bytes], program)
    return b58encode(address)
