# Copyright (c) 2026 Ansel
# SPDX-License-Identifier: MPL-2.0
"""Small, bounded account profile validators shared with the payment Worker.

Avatars are inline PNG, baseline JPEG or static WebP only. PNG scanlines and
baseline JPEG entropy are checked without a production image-library dependency.
WebP validation checks its complete container and bitstream framing, not pixels.
"""
from __future__ import annotations

import base64
import binascii
import math
import re
import struct
import unicodedata
import zlib

MAX_AVATAR_BYTES = 65_536
MAX_AVATAR_DIMENSION = 1_024
MAX_PNG_SCAN_BYTES = 4_195_328
_DATA_URL = re.compile(r"data:image/(png|jpeg|webp);base64,([A-Za-z0-9+/]*={0,2})\Z")


def normalize_display_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("昵称格式不正确")
    # Python 3.11 ships Unicode 14; these format characters were added in 15.
    if any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} or 0x13439 <= ord(char) <= 0x1343F for char in value):
        raise ValueError("昵称不能含控制或隐藏字符")
    result = value.strip()
    if not 1 <= len(result) <= 32:
        raise ValueError("昵称需为 1 至 32 个字符")
    return result


validate_display_name = normalize_display_name


def _fail() -> None:
    raise ValueError("头像图片无效，请重新选择")


def _dimensions(width: int, height: int) -> None:
    if not (1 <= width <= MAX_AVATAR_DIMENSION and 1 <= height <= MAX_AVATAR_DIMENSION):
        raise ValueError("头像尺寸不能超过 1024 × 1024")


def _png(data: bytes) -> None:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        _fail()
    offset, header, palette, ids, ended = 8, None, False, [], False
    idat_closed = False
    while offset < len(data):
        if offset + 12 > len(data):
            _fail()
        size = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data) or not re.fullmatch(rb"[A-Za-z]{4}", kind) or kind[2] & 32:
            _fail()
        content = data[offset + 8:offset + 8 + size]
        if zlib.crc32(kind + content) != struct.unpack_from(">I", data, end - 4)[0]:
            _fail()
        if header is None and kind != b"IHDR":
            _fail()
        if kind == b"IHDR":
            if header is not None or size != 13:
                _fail()
            header = struct.unpack(">IIBBBBB", content)
            width, height, depth, color, compression, filtering, interlace = header
            _dimensions(width, height)
            depths = {0: {1, 2, 4, 8, 16}, 2: {8, 16}, 3: {1, 2, 4, 8}, 4: {8, 16}, 6: {8, 16}}
            if depth not in depths.get(color, ()) or compression or filtering or interlace not in {0, 1}:
                _fail()
        elif kind == b"PLTE":
            if palette or ids or header[3] in {0, 4} or not size or size % 3 or size > 768:
                _fail()
            if header[3] == 3 and size // 3 > 1 << header[2]:
                _fail()
            palette = True
        elif kind == b"IDAT":
            if idat_closed or (header[3] == 3 and not palette):
                _fail()
            ids.append(content)
        elif kind == b"IEND":
            if size or not ids or end != len(data):
                _fail()
            ended = True
        elif kind in {b"acTL", b"fcTL", b"fdAT"} or not kind[0] & 32:
            # Animated images and unknown critical chunks are not account avatars.
            _fail()
        if ids and kind != b"IDAT":
            idat_closed = True
        offset = end
    if not ended or header is None:
        _fail()
    width, height, depth, color, _, _, interlace = header
    bits = depth * {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    passes = [(0, 0, 1, 1)] if not interlace else [
        (0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4),
        (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2),
    ]
    rows = []
    for x, y, dx, dy in passes:
        pw, ph = max(0, (width - x + dx - 1) // dx), max(0, (height - y + dy - 1) // dy)
        if pw and ph:
            rows.extend([(pw * bits + 7) // 8] * ph)
    expected = sum(row + 1 for row in rows)
    if expected > MAX_PNG_SCAN_BYTES:
        raise ValueError("头像解码大小超出限制")
    inflater = zlib.decompressobj()
    try:
        decoded = inflater.decompress(b"".join(ids), expected + 1)
    except zlib.error:
        _fail()
    if len(decoded) != expected or not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        _fail()
    position = 0
    for row in rows:
        if decoded[position] > 4:
            _fail()
        position += row + 1


class _JpegBits:
    def __init__(self, data: bytes):
        self.data, self.position = data, 0

    def read(self, count: int) -> int:
        if self.position + count > 8 * len(self.data):
            _fail()
        result = 0
        for _ in range(count):
            result = (result << 1) | ((self.data[self.position >> 3] >> (7 - (self.position & 7))) & 1)
            self.position += 1
        return result

    def finish(self) -> None:
        remaining = 8 * len(self.data) - self.position
        if remaining > 7 or (remaining and self.read(remaining) != (1 << remaining) - 1):
            _fail()


def _huffman(content: bytes, tables: dict) -> None:
    position = 0
    while position < len(content):
        if position + 17 > len(content):
            _fail()
        descriptor = content[position]
        category, identifier = descriptor >> 4, descriptor & 15
        counts = content[position + 1:position + 17]
        total = sum(counts)
        position += 17
        if category > 1 or identifier > 3 or not total or total > 256 or position + total > len(content):
            _fail()
        values = content[position:position + total]
        position += total
        codes, code, index = {}, 0, 0
        for length, count in enumerate(counts, 1):
            if code + count >= 1 << length:
                _fail()
            for _ in range(count):
                symbol = values[index]
                if category == 0 and symbol > 11:
                    _fail()
                if category == 1 and ((symbol & 15) > 10 or (not symbol & 15 and symbol not in {0, 240})):
                    _fail()
                codes[(length, code)] = symbol
                index += 1
                code += 1
            code <<= 1
        tables[(category, identifier)] = codes


def _jpeg_symbol(bits: _JpegBits, table: dict) -> int:
    code = 0
    for length in range(1, 17):
        code = (code << 1) | bits.read(1)
        value = table.get((length, code))
        if value is not None:
            return value
    _fail()


def _jpeg_scan(chunks: list[bytes], scan: list, components: dict, width: int, height: int, tables: dict, restart: int) -> None:
    maximum_h = max(value[0] for value in components.values())
    maximum_v = max(value[1] for value in components.values())
    if len(scan) == 1:
        horizontal, vertical, _ = components[scan[0][0]]
        mcus = math.ceil(width * horizontal / (8 * maximum_h)) * math.ceil(height * vertical / (8 * maximum_v))
        blocks = [(scan[0], 1)]
    else:
        mcus = math.ceil(width / (8 * maximum_h)) * math.ceil(height / (8 * maximum_v))
        blocks = [(item, components[item[0]][0] * components[item[0]][1]) for item in scan]
    required_chunks = math.ceil(mcus / restart) if restart else 1
    if len(chunks) != required_chunks:
        _fail()
    completed = 0
    for chunk in chunks:
        bits = _JpegBits(chunk)
        count = min(restart, mcus - completed) if restart else mcus
        for _ in range(count):
            for (_, dc, ac), block_count in blocks:
                for _ in range(block_count):
                    bits.read(_jpeg_symbol(bits, tables[(0, dc)]))
                    coefficient = 1
                    while coefficient < 64:
                        symbol = _jpeg_symbol(bits, tables[(1, ac)])
                        run, size = symbol >> 4, symbol & 15
                        if not size:
                            if not run:
                                break
                            coefficient += 16
                            if coefficient > 64:
                                _fail()
                        else:
                            coefficient += run
                            if coefficient >= 64:
                                _fail()
                            bits.read(size)
                            coefficient += 1
        bits.finish()
        completed += count


def _jpeg(data: bytes) -> None:
    if data[:2] != b"\xff\xd8":
        _fail()
    position, dimensions, components, tables, quantizers, covered = 2, None, {}, {}, set(), set()
    restart, scans, ended = 0, 0, False
    while position < len(data):
        if data[position] != 255:
            _fail()
        while position < len(data) and data[position] == 255:
            position += 1
        if position == len(data):
            _fail()
        marker = data[position]
        position += 1
        if marker == 217:
            ended = position == len(data)
            break
        if marker in {0, 1, 216} or 208 <= marker <= 215 or position + 2 > len(data):
            _fail()
        size = int.from_bytes(data[position:position + 2], "big")
        if size < 2 or position + size > len(data):
            _fail()
        content = data[position + 2:position + size]
        position += size
        if marker == 192:
            if dimensions or len(content) < 9 or content[0] != 8:
                _fail()
            height, width, count = struct.unpack(">HHB", content[1:6])
            _dimensions(width, height)
            if not 1 <= count <= 4 or len(content) != 6 + 3 * count:
                _fail()
            for index in range(count):
                identifier, sampling, quantizer = content[6 + index * 3:9 + index * 3]
                horizontal, vertical = sampling >> 4, sampling & 15
                if identifier in components or not 1 <= horizontal <= 4 or not 1 <= vertical <= 4 or quantizer > 3:
                    _fail()
                components[identifier] = (horizontal, vertical, quantizer)
            if sum(h * v for h, v, _ in components.values()) > 10:
                _fail()
            dimensions = (width, height)
        elif marker == 219:
            index = 0
            while index < len(content):
                descriptor = content[index]
                index += 1
                precision, identifier = descriptor >> 4, descriptor & 15
                count = 64 * (precision + 1)
                if precision > 1 or identifier > 3 or index + count > len(content):
                    _fail()
                values = content[index:index + count]
                if precision == 0 and 0 in values or precision == 1 and any(values[j:j + 2] == b"\0\0" for j in range(0, count, 2)):
                    _fail()
                quantizers.add(identifier)
                index += count
        elif marker == 196:
            _huffman(content, tables)
        elif marker == 221:
            if len(content) != 2:
                _fail()
            restart = int.from_bytes(content, "big")
        elif marker == 218:
            if dimensions is None or not content or not 1 <= content[0] <= len(components) or len(content) != 4 + 2 * content[0] or content[-3:] != b"\x00\x3f\x00":
                _fail()
            scan = []
            for index in range(content[0]):
                identifier, selectors = content[1 + 2 * index:3 + 2 * index]
                dc, ac = selectors >> 4, selectors & 15
                if identifier not in components or identifier in covered or any(item[0] == identifier for item in scan) or (0, dc) not in tables or (1, ac) not in tables or components[identifier][2] not in quantizers:
                    _fail()
                scan.append((identifier, dc, ac))
            chunks, current, next_restart = [], bytearray(), 0
            while position < len(data):
                value = data[position]
                position += 1
                if value != 255:
                    current.append(value)
                    continue
                start = position - 1
                while position < len(data) and data[position] == 255:
                    position += 1
                if position == len(data):
                    _fail()
                escape = data[position]
                if escape == 0:
                    if position != start + 1:
                        _fail()
                    current.append(255)
                    position += 1
                elif 208 <= escape <= 215:
                    if not restart or escape != 208 + next_restart:
                        _fail()
                    next_restart = (next_restart + 1) % 8
                    chunks.append(bytes(current))
                    current = bytearray()
                    position += 1
                else:
                    position = start
                    break
            chunks.append(bytes(current))
            _jpeg_scan(chunks, scan, components, *dimensions, tables, restart)
            covered.update(item[0] for item in scan)
            scans += 1
        elif marker == 194:
            raise ValueError("渐进式 JPEG 请先重编码为普通图片")
        elif not (224 <= marker <= 239 or marker == 254):
            # Browser re-encoding yields baseline JPEG; reject unsupported coding.
            _fail()
    if not ended or not scans or covered != set(components):
        _fail()


class _WebpBool:
    def __init__(self, data: bytes):
        if len(data) < 2:
            _fail()
        self.data, self.position, self.range, self.value, self.count = data, 2, 255, int.from_bytes(data[:2], "big"), 0

    def read(self, count: int = 1) -> int:
        result = 0
        for _ in range(count):
            split = 1 + ((self.range - 1) >> 1)
            bit = self.value >= split << 8
            if bit:
                self.value -= split << 8
                self.range -= split
            else:
                self.range = split
            while self.range < 128:
                self.range <<= 1
                self.value <<= 1
                self.count += 1
                if self.count == 8:
                    if self.position >= len(self.data):
                        _fail()
                    self.value |= self.data[self.position]
                    self.position += 1
                    self.count = 0
            result = (result << 1) | bit
        return result


def _vp8(data: bytes) -> tuple[int, int]:
    if len(data) < 15 or data[3:6] != b"\x9d\x01\x2a":
        _fail()
    tag = int.from_bytes(data[:3], "little")
    if tag & 1 or (tag >> 1) & 7 > 3 or not tag & 16:
        _fail()
    partition = tag >> 5
    width, height = struct.unpack("<HH", data[6:10])
    if width & 0xC000 or height & 0xC000:
        _fail()
    _dimensions(width, height)
    if partition < 2 or 10 + partition >= len(data):
        _fail()
    bits = _WebpBool(data[10:10 + partition])
    if bits.read():
        _fail()
    bits.read()  # Clamp type.
    if bits.read():  # Segmentation.
        update_map, update_data = bits.read(), bits.read()
        if update_data:
            bits.read()
            for width_bits in (7, 6):
                for _ in range(4):
                    if bits.read():
                        bits.read(width_bits)
                        bits.read()
        if update_map:
            for _ in range(3):
                if bits.read():
                    bits.read(8)
    bits.read(); bits.read(6); bits.read(3)
    if bits.read() and bits.read():
        for _ in range(8):
            if bits.read():
                bits.read(6); bits.read()
    partitions = 1 << bits.read(2)
    cursor = 10 + partition
    length_table = 3 * (partitions - 1)
    if cursor + length_table >= len(data):
        _fail()
    sizes = [int.from_bytes(data[cursor + 3 * index:cursor + 3 * index + 3], "little") for index in range(partitions - 1)]
    cursor += length_table
    for size in sizes:
        if size < 1 or cursor + size >= len(data):
            _fail()
        cursor += size
    if cursor >= len(data):
        _fail()
    return width, height


def _webp(data: bytes) -> None:
    if len(data) < 20 or data[:4] != b"RIFF" or data[8:12] != b"WEBP" or int.from_bytes(data[4:8], "little") != len(data) - 8:
        _fail()
    cursor, extended, image, alpha, seen = 12, None, None, False, set()
    while cursor < len(data):
        if cursor + 8 > len(data):
            _fail()
        kind = data[cursor:cursor + 4]
        size = int.from_bytes(data[cursor + 4:cursor + 8], "little")
        end = cursor + 8 + size
        padded = end + (size & 1)
        if padded > len(data) or padded != end and data[end] != 0 or kind in seen:
            _fail()
        content = data[cursor + 8:end]
        seen.add(kind)
        if kind == b"VP8X":
            if cursor != 12 or size != 10 or content[0] & 0xC3 or content[1:4] != b"\0\0\0":
                _fail()
            width, height = 1 + int.from_bytes(content[4:7], "little"), 1 + int.from_bytes(content[7:10], "little")
            _dimensions(width, height)
            extended = (width, height, content[0])
        elif kind == b"VP8 ":
            if image is not None:
                _fail()
            image = _vp8(content)
        elif kind == b"VP8L":
            if image is not None or alpha or size < 8 or content[0] != 47:
                _fail()
            header = int.from_bytes(content[1:5], "little")
            if header >> 29 or not any(content[5:]):
                _fail()
            image = (1 + (header & 0x3FFF), 1 + ((header >> 14) & 0x3FFF))
            _dimensions(*image)
        elif kind == b"ALPH":
            if not extended or image or not size or content[0] & 0xC0 or content[0] & 3 > 1 or (content[0] >> 4) & 3 > 1:
                _fail()
            if not content[0] & 3 and size != 1 + extended[0] * extended[1]:
                _fail()
            if content[0] & 3 and size < 3:
                _fail()
            alpha = True
        elif kind in {b"ICCP", b"EXIF", b"XMP "}:
            if extended is None or not size:
                _fail()
        else:
            _fail()
        cursor = padded
    if image is None or extended and image != extended[:2]:
        _fail()
    if extended:
        flags = extended[2]
        if bool(flags & 32) != (b"ICCP" in seen) or bool(flags & 8) != (b"EXIF" in seen) or bool(flags & 4) != (b"XMP " in seen) or alpha and not flags & 16:
            _fail()


def validate_avatar(value: object) -> str | None:
    """None leaves the avatar unchanged; empty string explicitly clears it."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("头像格式不正确")
    if value == "":
        return ""
    # Bound the string before regex/base64 allocation as well as decoded bytes.
    if len(value) > 40 + 4 * ((MAX_AVATAR_BYTES + 2) // 3):
        raise ValueError("头像不能超过 64 KiB")
    match = _DATA_URL.fullmatch(value)
    if not match or not match[2]:
        _fail()
    try:
        decoded = base64.b64decode(match[2], validate=True)
    except (ValueError, binascii.Error):
        _fail()
    if not decoded or len(decoded) > MAX_AVATAR_BYTES:
        raise ValueError("头像不能超过 64 KiB")
    if base64.b64encode(decoded).decode("ascii") != match[2]:
        _fail()
    {"png": _png, "jpeg": _jpeg, "webp": _webp}[match[1]](decoded)
    return value
