"""Immutable edit-surface inspection and deterministic retouch masks.

The WebUI draws only in the normalized coordinate space declared here.  The
source digest, raster orientation and dimensions are therefore part of the
backend contract instead of assumptions made from a mutable preview rendition.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct
import zlib
from typing import Any, Iterable, Mapping, Sequence


_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_JPEG_SOF = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)
_MAX_MASK_SIDE = 2048
_MAX_MASK_PIXELS = 4_194_304


@dataclass(frozen=True, slots=True)
class RasterDescriptor:
    width_px: int
    height_px: int
    orientation: int
    color_space: str
    mime_type: str

    def __post_init__(self) -> None:
        if self.width_px < 1 or self.height_px < 1:
            raise ValueError("raster dimensions must be positive")
        if self.width_px > 100_000 or self.height_px > 100_000:
            raise ValueError("raster dimensions exceed the supported limit")
        if self.orientation not in range(1, 9):
            raise ValueError("raster orientation must be between 1 and 8")


@dataclass(frozen=True, slots=True)
class CompiledMask:
    width_px: int
    height_px: int
    png_bytes: bytes
    sha256: str
    covered_fraction: float
    pixel_regions: tuple[dict[str, int], ...]

    def to_metadata(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "coordinate_space_version": "oriented-normalized-v1",
            "width_px": self.width_px,
            "height_px": self.height_px,
            "sha256": self.sha256,
            "size_bytes": len(self.png_bytes),
            "covered_fraction": self.covered_fraction,
            "pixel_regions": [dict(region) for region in self.pixel_regions],
        }


def inspect_raster(content: bytes, mime_type: str) -> RasterDescriptor:
    normalized = str(mime_type or "").split(";", 1)[0].strip().casefold()
    if normalized == "image/png":
        return _inspect_png(content)
    if normalized == "image/jpeg":
        return _inspect_jpeg(content)
    if normalized == "image/gif":
        return _inspect_gif(content)
    if normalized == "image/webp":
        return _inspect_webp(content)
    raise ValueError(f"retouch edit surfaces do not support {normalized or 'this media type'}")


def _inspect_png(content: bytes) -> RasterDescriptor:
    if len(content) < 33 or not content.startswith(_PNG_SIGNATURE):
        raise ValueError("PNG edit surface is malformed")
    length = struct.unpack(">I", content[8:12])[0]
    if length != 13 or content[12:16] != b"IHDR":
        raise ValueError("PNG edit surface has no valid IHDR")
    width, height, bit_depth, color_type = struct.unpack(">IIBB", content[16:26])
    if bit_depth not in {1, 2, 4, 8, 16} or color_type not in {0, 2, 3, 4, 6}:
        raise ValueError("PNG edit surface uses an unsupported color layout")
    color_space = {
        0: "gray",
        2: "srgb",
        3: "indexed-srgb",
        4: "gray-alpha",
        6: "srgb-alpha",
    }[color_type]
    return RasterDescriptor(width, height, 1, color_space, "image/png")


def _jpeg_segments(content: bytes) -> Iterable[tuple[int, bytes]]:
    if len(content) < 4 or content[:2] != b"\xff\xd8":
        raise ValueError("JPEG edit surface is malformed")
    offset = 2
    while offset + 1 < len(content):
        if content[offset] != 0xFF:
            offset += 1
            continue
        while offset < len(content) and content[offset] == 0xFF:
            offset += 1
        if offset >= len(content):
            break
        marker = content[offset]
        offset += 1
        if marker in {0x01, 0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
            if marker == 0xD9:
                break
            continue
        if offset + 2 > len(content):
            raise ValueError("JPEG edit surface has a truncated segment")
        length = struct.unpack(">H", content[offset : offset + 2])[0]
        if length < 2 or offset + length > len(content):
            raise ValueError("JPEG edit surface has an invalid segment length")
        payload = content[offset + 2 : offset + length]
        yield marker, payload
        offset += length
        if marker == 0xDA:
            break


def _exif_orientation(payload: bytes) -> int:
    if not payload.startswith(b"Exif\x00\x00") or len(payload) < 14:
        return 1
    tiff = payload[6:]
    byte_order = tiff[:2]
    if byte_order == b"II":
        endian = "<"
    elif byte_order == b"MM":
        endian = ">"
    else:
        return 1
    try:
        if struct.unpack(endian + "H", tiff[2:4])[0] != 42:
            return 1
        ifd_offset = struct.unpack(endian + "I", tiff[4:8])[0]
        if ifd_offset + 2 > len(tiff):
            return 1
        count = struct.unpack(endian + "H", tiff[ifd_offset : ifd_offset + 2])[0]
        for index in range(min(count, 256)):
            start = ifd_offset + 2 + index * 12
            entry = tiff[start : start + 12]
            if len(entry) < 12:
                return 1
            tag, value_type, value_count = struct.unpack(endian + "HHI", entry[:8])
            if tag == 0x0112 and value_type == 3 and value_count == 1:
                orientation = struct.unpack(endian + "H", entry[8:10])[0]
                return orientation if orientation in range(1, 9) else 1
    except struct.error:
        return 1
    return 1


def _inspect_jpeg(content: bytes) -> RasterDescriptor:
    orientation = 1
    width = height = components = 0
    for marker, payload in _jpeg_segments(content):
        if marker == 0xE1:
            orientation = _exif_orientation(payload)
        elif marker in _JPEG_SOF:
            if len(payload) < 6:
                raise ValueError("JPEG edit surface has a malformed frame header")
            height, width = struct.unpack(">HH", payload[1:5])
            components = payload[5]
    if not width or not height:
        raise ValueError("JPEG edit surface has no supported frame")
    if orientation in {5, 6, 7, 8}:
        width, height = height, width
    color_space = {1: "gray", 3: "srgb", 4: "cmyk"}.get(components, "unknown")
    return RasterDescriptor(width, height, orientation, color_space, "image/jpeg")


def _inspect_gif(content: bytes) -> RasterDescriptor:
    if len(content) < 10 or content[:6] not in {b"GIF87a", b"GIF89a"}:
        raise ValueError("GIF edit surface is malformed")
    width, height = struct.unpack("<HH", content[6:10])
    return RasterDescriptor(width, height, 1, "indexed-srgb", "image/gif")


def _inspect_webp(content: bytes) -> RasterDescriptor:
    if len(content) < 30 or content[:4] != b"RIFF" or content[8:12] != b"WEBP":
        raise ValueError("WebP edit surface is malformed")
    kind = content[12:16]
    payload = content[20:]
    if kind == b"VP8X" and len(payload) >= 10:
        if payload[0] & 0x02:
            raise ValueError("animated WebP cannot be used as a precise-retouch surface")
        width = 1 + int.from_bytes(payload[4:7], "little")
        height = 1 + int.from_bytes(payload[7:10], "little")
    elif kind == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = int.from_bytes(payload[1:5], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
    elif kind == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        width = struct.unpack("<H", payload[6:8])[0] & 0x3FFF
        height = struct.unpack("<H", payload[8:10])[0] & 0x3FFF
    else:
        raise ValueError("WebP edit surface uses an unsupported frame layout")
    return RasterDescriptor(width, height, 1, "srgb-alpha", "image/webp")


def compile_annotation_mask(
    width_px: int,
    height_px: int,
    annotations: Sequence[Mapping[str, Any]],
) -> CompiledMask:
    """Rasterize structured normalized geometry to a bounded grayscale PNG.

    The bounded mask is deterministic validation evidence and a gateway-ready
    ROI plane.  It intentionally contains no user path or prompt text.
    """

    if width_px < 1 or height_px < 1:
        raise ValueError("edit surface dimensions must be positive")
    scale = min(
        1.0,
        _MAX_MASK_SIDE / max(width_px, height_px),
        math.sqrt(
            _MAX_MASK_PIXELS
            / (width_px * height_px * max(1, len(annotations)))
        ),
    )
    mask_width = max(1, round(width_px * scale))
    mask_height = max(1, round(height_px * scale))
    pixels = bytearray(mask_width * mask_height)
    regions: list[dict[str, int]] = []
    for annotation in annotations:
        kind = str(annotation.get("kind", ""))
        geometry = annotation.get("normalized_geometry")
        if not isinstance(geometry, Mapping):
            raise ValueError("annotation normalized_geometry must be an object")
        bounds = _normalized_bounds(kind, geometry)
        pixel_bounds = _pixel_bounds(bounds, mask_width, mask_height)
        regions.append(pixel_bounds)
        _paint(pixels, mask_width, mask_height, kind, geometry, pixel_bounds)
    png = _encode_gray_png(mask_width, mask_height, pixels)
    covered = sum(1 for value in pixels if value) / len(pixels)
    return CompiledMask(
        width_px=mask_width,
        height_px=mask_height,
        png_bytes=png,
        sha256=hashlib.sha256(png).hexdigest(),
        covered_fraction=round(covered, 8),
        pixel_regions=tuple(regions),
    )


def _points(geometry: Mapping[str, Any]) -> list[tuple[float, float]]:
    value = geometry.get("points")
    if not isinstance(value, list):
        return []
    return [(float(item["x"]), float(item["y"])) for item in value]


def _normalized_bounds(kind: str, geometry: Mapping[str, Any]) -> tuple[float, float, float, float]:
    if kind in {"rectangle", "ellipse"}:
        x = float(geometry["x"])
        y = float(geometry["y"])
        return x, y, x + float(geometry["width"]), y + float(geometry["height"])
    if kind == "point":
        x, y = float(geometry["x"]), float(geometry["y"])
        return max(0.0, x - 0.01), max(0.0, y - 0.01), min(1.0, x + 0.01), min(1.0, y + 0.01)
    points = _points(geometry)
    if not points:
        raise ValueError(f"{kind} annotation has no points")
    pad = float(geometry.get("width", 0.01)) / 2 if kind in {"polyline", "brush"} else 0.0
    return (
        max(0.0, min(point[0] for point in points) - pad),
        max(0.0, min(point[1] for point in points) - pad),
        min(1.0, max(point[0] for point in points) + pad),
        min(1.0, max(point[1] for point in points) + pad),
    )


def _pixel_bounds(
    bounds: tuple[float, float, float, float], width: int, height: int
) -> dict[str, int]:
    left = max(0, min(width - 1, math.floor(bounds[0] * width)))
    top = max(0, min(height - 1, math.floor(bounds[1] * height)))
    right = max(left + 1, min(width, math.ceil(bounds[2] * width)))
    bottom = max(top + 1, min(height, math.ceil(bounds[3] * height)))
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def _paint(
    pixels: bytearray,
    width: int,
    height: int,
    kind: str,
    geometry: Mapping[str, Any],
    bounds: Mapping[str, int],
) -> None:
    left, top = bounds["x"], bounds["y"]
    right, bottom = left + bounds["width"], top + bounds["height"]
    points = _points(geometry)
    stroke = max(1.0, float(geometry.get("width", 0.01)) * min(width, height))
    if kind in {"rectangle", "point"}:
        fill = b"\xff" * (right - left)
        for py in range(top, bottom):
            start = py * width + left
            pixels[start : start + len(fill)] = fill
        return
    for py in range(top, bottom):
        ny = (py + 0.5) / height
        for px in range(left, right):
            nx = (px + 0.5) / width
            inside = False
            if kind == "ellipse":
                cx = float(geometry["x"]) + float(geometry["width"]) / 2
                cy = float(geometry["y"]) + float(geometry["height"]) / 2
                rx = float(geometry["width"]) / 2
                ry = float(geometry["height"]) / 2
                inside = ((nx - cx) / rx) ** 2 + ((ny - cy) / ry) ** 2 <= 1
            elif kind == "polygon":
                inside = _inside_polygon(nx, ny, points)
            elif kind in {"polyline", "brush"}:
                inside = any(
                    _distance_to_segment(px + 0.5, py + 0.5, ax * width, ay * height, bx * width, by * height)
                    <= stroke / 2
                    for (ax, ay), (bx, by) in zip(points, points[1:])
                )
            if inside:
                pixels[py * width + px] = 255


def _inside_polygon(x: float, y: float, points: Sequence[tuple[float, float]]) -> bool:
    inside = False
    previous = points[-1]
    for current in points:
        ax, ay = previous
        bx, by = current
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
        previous = current
    return inside


def _distance_to_segment(
    px: float, py: float, ax: float, ay: float, bx: float, by: float
) -> float:
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)


def _encode_gray_png(width: int, height: int, pixels: bytes) -> bytes:
    rows = b"".join(b"\x00" + pixels[index * width : (index + 1) * width] for index in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return _PNG_SIGNATURE + _png_chunk(b"IHDR", header) + _png_chunk(b"IDAT", zlib.compress(rows, 9)) + _png_chunk(b"IEND", b"")


__all__ = ["CompiledMask", "RasterDescriptor", "compile_annotation_mask", "inspect_raster"]
