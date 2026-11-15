# SPDX-License-Identifier: GPL-3.0-or-later
"""Small DDS-to-PNG decoder for the accessory picker previews.

Supports common uncompressed RGB(A) DDS images and DXT1/BC1, DXT3/BC2, and
DXT5/BC3. It uses only the Python standard library so it also works in
Blender's bundled Python without Pillow or an image plug-in.
"""
from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _channel(pixel: int, mask: int, default: int) -> int:
    if not mask:
        return default
    shift = (mask & -mask).bit_length() - 1
    value = (pixel & mask) >> shift
    maximum = mask >> shift
    return (value * 255 + maximum // 2) // maximum


def _rgb565(value: int) -> tuple[int, int, int]:
    return (((value >> 11) & 31) * 255 // 31,
            ((value >> 5) & 63) * 255 // 63,
            (value & 31) * 255 // 31)


def _color_table(c0: int, c1: int, allow_transparency: bool):
    a, b = _rgb565(c0), _rgb565(c1)
    if c0 > c1 or not allow_transparency:
        return (a, b,
                tuple((2 * a[i] + b[i]) // 3 for i in range(3)),
                tuple((a[i] + 2 * b[i]) // 3 for i in range(3)))
    return (a, b, tuple((a[i] + b[i]) // 2 for i in range(3)), None)


def _decode_blocks(data: bytes, width: int, height: int, kind: bytes) -> bytes:
    block_bytes = 8 if kind == b"DXT1" else 16
    pixels = bytearray(width * height * 4)
    offset = 0
    for by in range((height + 3) // 4):
        for bx in range((width + 3) // 4):
            block = data[offset:offset + block_bytes]
            if len(block) != block_bytes:
                raise ValueError("truncated DDS block data")
            offset += block_bytes
            if kind == b"DXT1":
                color_data = block
                alpha_values = [255] * 16
            elif kind == b"DXT3":
                alpha_bits = int.from_bytes(block[:8], "little")
                alpha_values = [((alpha_bits >> (4 * i)) & 15) * 17 for i in range(16)]
                color_data = block[8:]
            else:  # DXT5 / BC3
                a0, a1 = block[0], block[1]
                if a0 > a1:
                    alpha_table = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
                else:
                    alpha_table = [a0, a1] + [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)] + [0, 255]
                alpha_bits = int.from_bytes(block[2:8], "little")
                alpha_values = [alpha_table[(alpha_bits >> (3 * i)) & 7] for i in range(16)]
                color_data = block[8:]
            c0, c1, selectors = struct.unpack("<HHI", color_data)
            colors = _color_table(c0, c1, kind == b"DXT1")
            for i in range(16):
                x, y = bx * 4 + i % 4, by * 4 + i // 4
                if x >= width or y >= height:
                    continue
                color = colors[(selectors >> (2 * i)) & 3]
                at = (y * width + x) * 4
                if color is None:
                    pixels[at:at + 4] = b"\0\0\0\0"
                else:
                    pixels[at:at + 4] = bytes((*color, alpha_values[i]))
    return bytes(pixels)


def decode_dds(path: str | Path) -> tuple[int, int, bytes]:
    data = Path(path).read_bytes()
    if len(data) < 128 or data[:4] != b"DDS " or struct.unpack_from("<I", data, 4)[0] != 124:
        raise ValueError("invalid DDS header")
    height, width = struct.unpack_from("<II", data, 12)
    if not width or not height or width * height > 100_000_000:
        raise ValueError("invalid DDS dimensions")
    pf_flags, fourcc, bits = struct.unpack_from("<I4sI", data, 80)
    if pf_flags & 0x4:
        if fourcc not in (b"DXT1", b"DXT3", b"DXT5"):
            raise ValueError(f"unsupported DDS compression {fourcc!r}")
        return width, height, _decode_blocks(data[128:], width, height, fourcc)
    if not pf_flags & 0x40 or bits not in (16, 24, 32):
        raise ValueError(f"unsupported DDS pixel format ({bits} bits)")
    red, green, blue, alpha = struct.unpack_from("<IIII", data, 92)
    header_flags = struct.unpack_from("<I", data, 8)[0]
    pitch = struct.unpack_from("<I", data, 20)[0] if header_flags & 0x8 else 0
    bytes_per_pixel = bits // 8
    if pitch < width * bytes_per_pixel:
        pitch = width * bytes_per_pixel
    pixels = bytearray(width * height * 4)
    start = 128
    for y in range(height):
        row = start + y * pitch
        for x in range(width):
            at = row + x * bytes_per_pixel
            value = int.from_bytes(data[at:at + bytes_per_pixel], "little")
            out = (y * width + x) * 4
            pixels[out:out + 4] = bytes((_channel(value, red, 0), _channel(value, green, 0),
                                         _channel(value, blue, 0), _channel(value, alpha, 255)))
    return width, height, bytes(pixels)


def write_png(path: str | Path, width: int, height: int, rgba: bytes) -> None:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body) & 0xffffffff)

    rows = b"".join(b"\0" + rgba[y * width * 4:(y + 1) * width * 4] for y in range(height))
    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">2I5B", width, height, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(rows, 6)) + chunk(b"IEND", b""))
    Path(path).write_bytes(png)


def dds_to_png(source: str | Path, target: str | Path) -> None:
    width, height, rgba = decode_dds(source)
    write_png(target, width, height, rgba)
