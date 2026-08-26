"""QQ Music QRC decryption and conversion helpers.

The block cipher implementation is a Python adaptation of Robotxm's
GPL-3.0 QRC decryptor, which in turn acknowledges LDDC.  QQ Music's QRC
cipher uses a historical DES implementation whose byte ordering is not
compatible with a stock DES3 primitive.

Source: https://github.com/Robotxm/ESLyric-LyricsSource
License: GPL-3.0
"""

from __future__ import annotations

import html
import re
import zlib

DECRYPT = 0
ENCRYPT = 1
QRC_KEY = b"!@#)(*$%123ZXC!@!@#)(NHL"

SBOX = (
    (14, 4, 13, 1, 2, 15, 11, 8, 3, 10, 6, 12, 5, 9, 0, 7, 0, 15, 7, 4, 14, 2, 13, 1, 10, 6, 12, 11, 9, 5, 3, 8, 4, 1, 14, 8, 13, 6, 2, 11, 15, 12, 9, 7, 3, 10, 5, 0, 15, 12, 8, 2, 4, 9, 1, 7, 5, 11, 3, 14, 10, 0, 6, 13),
    (15, 1, 8, 14, 6, 11, 3, 4, 9, 7, 2, 13, 12, 0, 5, 10, 3, 13, 4, 7, 15, 2, 8, 15, 12, 0, 1, 10, 6, 9, 11, 5, 0, 14, 7, 11, 10, 4, 13, 1, 5, 8, 12, 6, 9, 3, 2, 15, 13, 8, 10, 1, 3, 15, 4, 2, 11, 6, 7, 12, 0, 5, 14, 9),
    (10, 0, 9, 14, 6, 3, 15, 5, 1, 13, 12, 7, 11, 4, 2, 8, 13, 7, 0, 9, 3, 4, 6, 10, 2, 8, 5, 14, 12, 11, 15, 1, 13, 6, 4, 9, 8, 15, 3, 0, 11, 1, 2, 12, 5, 10, 14, 7, 1, 10, 13, 0, 6, 9, 8, 7, 4, 15, 14, 3, 11, 5, 2, 12),
    (7, 13, 14, 3, 0, 6, 9, 10, 1, 2, 8, 5, 11, 12, 4, 15, 13, 8, 11, 5, 6, 15, 0, 3, 4, 7, 2, 12, 1, 10, 14, 9, 10, 6, 9, 0, 12, 11, 7, 13, 15, 1, 3, 14, 5, 2, 8, 4, 3, 15, 0, 6, 10, 10, 13, 8, 9, 4, 5, 11, 12, 7, 2, 14),
    (2, 12, 4, 1, 7, 10, 11, 6, 8, 5, 3, 15, 13, 0, 14, 9, 14, 11, 2, 12, 4, 7, 13, 1, 5, 0, 15, 10, 3, 9, 8, 6, 4, 2, 1, 11, 10, 13, 7, 8, 15, 9, 12, 5, 6, 3, 0, 14, 11, 8, 12, 7, 1, 14, 2, 13, 6, 15, 0, 9, 10, 4, 5, 3),
    (12, 1, 10, 15, 9, 2, 6, 8, 0, 13, 3, 4, 14, 7, 5, 11, 10, 15, 4, 2, 7, 12, 9, 5, 6, 1, 13, 14, 0, 11, 3, 8, 9, 14, 15, 5, 2, 8, 12, 3, 7, 0, 4, 10, 1, 13, 11, 6, 4, 3, 2, 12, 9, 5, 15, 10, 11, 14, 1, 7, 6, 0, 8, 13),
    (4, 11, 2, 14, 15, 0, 8, 13, 3, 12, 9, 7, 5, 10, 6, 1, 13, 0, 11, 7, 4, 9, 1, 10, 14, 3, 5, 12, 2, 15, 8, 6, 1, 4, 11, 13, 12, 3, 7, 14, 10, 15, 6, 8, 0, 5, 9, 2, 6, 11, 13, 8, 1, 4, 10, 7, 9, 5, 0, 15, 14, 2, 3, 12),
    (13, 2, 8, 4, 6, 15, 11, 1, 10, 9, 3, 14, 5, 0, 12, 7, 1, 15, 13, 8, 10, 3, 7, 4, 12, 5, 6, 11, 0, 14, 9, 2, 7, 11, 4, 1, 9, 12, 14, 2, 0, 6, 10, 13, 15, 3, 5, 8, 2, 1, 14, 7, 4, 10, 8, 13, 15, 12, 9, 0, 3, 5, 6, 11),
)

KEY_RND_SHIFT = (1, 1, 2, 2, 2, 2, 2, 2, 1, 2, 2, 2, 2, 2, 2, 1)
KEY_PERM_C = (56, 48, 40, 32, 24, 16, 8, 0, 57, 49, 41, 33, 25, 17, 9, 1, 58, 50, 42, 34, 26, 18, 10, 2, 59, 51, 43, 35)
KEY_PERM_D = (62, 54, 46, 38, 30, 22, 14, 6, 61, 53, 45, 37, 29, 21, 13, 5, 60, 52, 44, 36, 28, 20, 12, 4, 27, 19, 11, 3)
KEY_COMPRESSION = (13, 16, 10, 23, 0, 4, 2, 27, 14, 5, 20, 9, 22, 18, 11, 3, 25, 7, 15, 6, 26, 19, 12, 1, 40, 51, 30, 36, 46, 54, 29, 39, 50, 44, 32, 47, 43, 48, 38, 55, 33, 52, 45, 41, 49, 35, 28, 31)


def _bitnum(value: bytes | bytearray, bit: int, shift: int) -> int:
    index = (bit // 32) * 4 + 3 - (bit % 32) // 8
    return ((value[index] >> (7 - bit % 8)) & 1) << shift


def _bitnum_intr(value: int, bit: int, shift: int) -> int:
    return ((value >> (31 - bit)) & 1) << shift


def _bitnum_intl(value: int, bit: int, shift: int) -> int:
    return ((value << bit) & 0x80000000) >> shift


def _sbox_bit(value: int) -> int:
    return (value & 32) | ((value & 31) >> 1) | ((value & 1) << 4)


def _initial_permutation(data: bytes | bytearray) -> tuple[int, int]:
    left_indexes = (57, 49, 41, 33, 25, 17, 9, 1, 59, 51, 43, 35, 27, 19, 11, 3, 61, 53, 45, 37, 29, 21, 13, 5, 63, 55, 47, 39, 31, 23, 15, 7)
    right_indexes = (56, 48, 40, 32, 24, 16, 8, 0, 58, 50, 42, 34, 26, 18, 10, 2, 60, 52, 44, 36, 28, 20, 12, 4, 62, 54, 46, 38, 30, 22, 14, 6)
    left = sum(_bitnum(data, bit, 31 - index) for index, bit in enumerate(left_indexes))
    right = sum(_bitnum(data, bit, 31 - index) for index, bit in enumerate(right_indexes))
    return left, right


def _inverse_permutation(s0: int, s1: int) -> bytearray:
    data = bytearray(8)
    pairs = (
        (3, 7), (2, 6), (1, 5), (0, 4),
        (7, 3), (6, 2), (5, 1), (4, 0),
    )
    for target, base in pairs:
        data[target] = sum(
            _bitnum_intr(s1 if offset % 2 == 0 else s0, base + 8 * (offset // 2), 7 - offset)
            for offset in range(8)
        )
    return data


def _f(state: int, key: list[int]) -> int:
    t1 = (_bitnum_intl(state, 31, 0) | ((state & 0xF0000000) >> 1) | _bitnum_intl(state, 4, 5) | _bitnum_intl(state, 3, 6) | ((state & 0x0F000000) >> 3) | _bitnum_intl(state, 8, 11) | _bitnum_intl(state, 7, 12) | ((state & 0x00F00000) >> 5) | _bitnum_intl(state, 12, 17) | _bitnum_intl(state, 11, 18) | ((state & 0x000F0000) >> 7) | _bitnum_intl(state, 16, 23))
    t2 = (_bitnum_intl(state, 15, 0) | ((state & 0x0000F000) << 15) | _bitnum_intl(state, 20, 5) | _bitnum_intl(state, 19, 6) | ((state & 0x00000F00) << 13) | _bitnum_intl(state, 24, 11) | _bitnum_intl(state, 23, 12) | ((state & 0x000000F0) << 11) | _bitnum_intl(state, 28, 17) | _bitnum_intl(state, 27, 18) | ((state & 0x0000000F) << 9) | _bitnum_intl(state, 0, 23))
    large = [
        ((t1 >> 24) & 0xFF) ^ key[0], ((t1 >> 16) & 0xFF) ^ key[1],
        ((t1 >> 8) & 0xFF) ^ key[2], ((t2 >> 24) & 0xFF) ^ key[3],
        ((t2 >> 16) & 0xFF) ^ key[4], ((t2 >> 8) & 0xFF) ^ key[5],
    ]
    mixed = (
        (SBOX[0][_sbox_bit(large[0] >> 2)] << 28)
        | (SBOX[1][_sbox_bit(((large[0] & 3) << 4) | (large[1] >> 4))] << 24)
        | (SBOX[2][_sbox_bit(((large[1] & 15) << 2) | (large[2] >> 6))] << 20)
        | (SBOX[3][_sbox_bit(large[2] & 63)] << 16)
        | (SBOX[4][_sbox_bit(large[3] >> 2)] << 12)
        | (SBOX[5][_sbox_bit(((large[3] & 3) << 4) | (large[4] >> 4))] << 8)
        | (SBOX[6][_sbox_bit(((large[4] & 15) << 2) | (large[5] >> 6))] << 4)
        | SBOX[7][_sbox_bit(large[5] & 63)]
    )
    permutation = (15, 6, 19, 20, 28, 11, 27, 16, 0, 14, 22, 25, 4, 17, 30, 9, 1, 7, 23, 13, 31, 26, 2, 8, 18, 12, 29, 5, 21, 10, 3, 24)
    return sum(_bitnum_intl(mixed, bit, index) for index, bit in enumerate(permutation))


def _crypt_block(data: bytes | bytearray, schedule: list[list[int]]) -> bytearray:
    s0, s1 = _initial_permutation(data)
    for index in range(15):
        previous = s1
        s1 = _f(s1, schedule[index]) ^ s0
        s0 = previous
    s0 = _f(s1, schedule[15]) ^ s0
    return _inverse_permutation(s0, s1)


def _key_schedule(key: bytes, mode: int) -> list[list[int]]:
    schedule = [[0] * 6 for _ in range(16)]
    c = sum(_bitnum(key, bit, 31 - index) for index, bit in enumerate(KEY_PERM_C))
    d = sum(_bitnum(key, bit, 31 - index) for index, bit in enumerate(KEY_PERM_D))
    for index, shift in enumerate(KEY_RND_SHIFT):
        c = ((c << shift) | (c >> (28 - shift))) & 0xFFFFFFF0
        d = ((d << shift) | (d >> (28 - shift))) & 0xFFFFFFF0
        target = 15 - index if mode == DECRYPT else index
        for position in range(24):
            schedule[target][position // 8] |= _bitnum_intr(c, KEY_COMPRESSION[position], 7 - position % 8)
        for position in range(24, 48):
            schedule[target][position // 8] |= _bitnum_intr(d, KEY_COMPRESSION[position] - 27, 7 - position % 8)
    return schedule


def _triple_des_schedule(key: bytes) -> list[list[list[int]]]:
    return [
        _key_schedule(key[16:24], DECRYPT),
        _key_schedule(key[8:16], ENCRYPT),
        _key_schedule(key[0:8], DECRYPT),
    ]


def decrypt_qrc(value: str) -> str:
    """Decrypt a cloud QRC hexadecimal payload to its text representation."""
    normalized = re.sub(r"\s+", "", str(value or ""))
    if not normalized or len(normalized) % 16 or not re.fullmatch(r"[0-9A-Fa-f]+", normalized):
        return ""
    encrypted = bytearray.fromhex(normalized)
    schedule = _triple_des_schedule(QRC_KEY)
    decrypted = bytearray()
    for offset in range(0, len(encrypted), 8):
        block: bytes | bytearray = encrypted[offset : offset + 8]
        for key in schedule:
            block = _crypt_block(block, key)
        decrypted.extend(block)
    try:
        return zlib.decompress(decrypted).decode("utf-8")
    except (UnicodeDecodeError, zlib.error):
        return ""


def _format_time(milliseconds: int) -> str:
    value = abs(int(milliseconds))
    minutes, remainder = divmod(value, 60_000)
    seconds, remainder = divmod(remainder, 1_000)
    return f"{minutes:02d}:{seconds:02d}.{remainder // 10:02d}"


def qrc_to_lrc(value: str) -> str:
    """Convert decrypted QRC XML/word timings into enhanced LRC."""
    text = str(value or "")
    if "<QrcInfos" not in text and "<Lyric_1" not in text:
        return text.strip()
    match = re.search(r'LyricContent="([\s\S]*?)"\s*/?>', text)
    if not match:
        return ""
    content = html.unescape(match.group(1))
    content = re.sub(
        r"^\[(\d+),(\d+)\]",
        lambda item: (
            f"[{_format_time(int(item.group(1)))}]"
            f"<{_format_time(int(item.group(1)))}>"
        ),
        content,
        flags=re.MULTILINE,
    )
    content = re.sub(
        r"\((\d+),(\d+)\)",
        lambda item: f"<{_format_time(int(item.group(1)) + int(item.group(2)))}>",
        content,
    )
    return content.strip()


def decrypt_qrc_to_lrc(value: str) -> str:
    return qrc_to_lrc(decrypt_qrc(value))
