"""Capture post-processing: downscale for the model, MCP image + resource link,
and pruning of the captures folder (last 50 files)."""

from __future__ import annotations

import base64
import io
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger("vectorworks_mcp.capture")

MAX_EDGE = 1568
KEEP = 50
INLINE_FALLBACK_LIMIT = 1_000_000


def new_capture_path(fmt: str) -> Path:
    directory = config.captures_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ext = "jpg" if fmt in {"jpg", "jpeg"} else "png"
    return directory / f"capture-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.{ext}"


def prune(keep: int = KEEP) -> None:
    directory = config.captures_dir()
    try:
        files = sorted((p for p in directory.iterdir() if p.is_file() and p.name.startswith("capture-")
                        and not p.name.endswith(".map.json")), key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return
    for old in files[keep:]:
        try:
            old.with_name(old.name + ".map.json").unlink(missing_ok=True)  # image_to_world sidecar
            old.unlink()
        except OSError:
            pass


def _png_size(data: bytes) -> Optional[tuple[int, int]]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return None


def _trim_background(img):
    """Crop uniform background margins (3D fits are approximate, sheets have
    paper around the drawing) so the model sees the drawing larger for the same
    token cost. The full-resolution file on disk is never modified. Returns
    (cropped image, [left, top, right, bottom]) or None when nothing to trim."""
    from PIL import Image, ImageChops
    rgb = img.convert("RGB")
    background = rgb.getpixel((0, 0))
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, background)).convert("L")
    box = diff.point(lambda value: 255 if value > 12 else 0).getbbox()
    if not box:
        return None
    width, height = rgb.size
    pad = max(8, int(0.03 * max(box[2] - box[0], box[3] - box[1])))
    box = (max(0, box[0] - pad), max(0, box[1] - pad), min(width, box[2] + pad), min(height, box[3] + pad))
    if (box[2] - box[0]) * (box[3] - box[1]) > 0.85 * width * height:
        return None
    return rgb.crop(box), list(box)


def model_image(path: Path, max_edge: int = MAX_EDGE) -> tuple[Optional[bytes], str, dict]:
    """Return (bytes, mime, info) sized for the model. Pillow is optional and lazily imported."""
    info: dict = {}
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, "", {"inline_error": f"cannot read capture: {exc}"}
    mime = "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
    try:
        from PIL import Image  # lazy: ~100 ms, only when a capture is returned
    except Exception:  # noqa: BLE001
        size = _png_size(raw)
        if size:
            info["width"], info["height"] = size
        if len(raw) <= INLINE_FALLBACK_LIMIT:
            return raw, mime, info
        info["inline_error"] = "capture too large to inline without Pillow; open the resource link"
        return None, mime, info
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            info["width"], info["height"] = img.size
            trimmed = _trim_background(img)
            if trimmed is not None:
                img, info["trimmed_box"] = trimmed
                raw = b""  # force re-encode of the trimmed image below
            long_edge = max(img.size)
            if raw and long_edge <= max_edge and len(raw) <= 2_000_000:
                return raw, mime, info
            scale = min(1.0, max_edge / float(long_edge))
            target = (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale)))
            small = img.convert("RGB") if img.mode not in {"RGB", "L"} else img
            small = small.resize(target, Image.LANCZOS)
            out = io.BytesIO()
            small.save(out, format="JPEG", quality=85, optimize=True)
            info["model_width"], info["model_height"] = target
            return out.getvalue(), "image/jpeg", info
    except Exception as exc:  # noqa: BLE001
        log.warning("downscale failed: %s", exc)
        if len(raw) <= INLINE_FALLBACK_LIMIT:
            return raw, mime, info
        return None, mime, {"inline_error": f"cannot downscale capture: {exc}"}


def content_blocks(path: Path) -> tuple[list[dict], dict]:
    data, mime, info = model_image(path)
    blocks: list[dict] = []
    if data is not None:
        blocks.append({"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime})
    uri = f"vw://capture/{path.name}"
    blocks.append({"type": "resource_link", "uri": uri, "name": path.name,
                   "mimeType": "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png",
                   "description": "Full-resolution Vectorworks capture"})
    info["resource"] = uri
    return blocks, info


_MAPPINGS: dict[str, dict] = {}  # capture file name -> image_to_world (this server session)
_LAST: list[str] = []


def image_mapping(name: str, native: dict, info: dict) -> Optional[dict]:
    """Pixel (u, v) of the image the model received -> drawing mm, from the bridge's world_bounds/px_per_mm
    plus the host's trim and downscale. None for 3D captures or old bridges."""
    wb, ppm = native.get("world_bounds"), native.get("px_per_mm")
    try:
        left, top, ppm = float(wb["left"]), float(wb["top"]), float(ppm)
    except (TypeError, KeyError, ValueError):
        return None
    if ppm <= 0:
        return None
    width, height = info.get("width") or native.get("width"), info.get("height") or native.get("height")
    box = info.get("trimmed_box") or [0, 0, width, height]
    scale = (info["model_width"] / float(box[2] - box[0])) if info.get("model_width") else 1.0
    mapping = {"x0": round(left + box[0] / ppm, 1), "y0": round(top - box[1] / ppm, 1),
               "mm_per_px": round(1.0 / (ppm * scale), 4),
               "rule": "x = x0 + u*mm_per_px, y = y0 - v*mm_per_px (u, v = pixel in the returned image)"}
    _MAPPINGS[name] = mapping
    _LAST[:] = [name]
    try:
        (config.captures_dir() / (name + ".map.json")).write_text(json.dumps(mapping), encoding="utf-8")
    except OSError:
        pass
    return mapping


def mapping_for(name: Optional[str]) -> Optional[dict]:
    """The image_to_world of a capture (default: the latest one this session)."""
    name = (name or (_LAST[0] if _LAST else "")).rsplit("/", 1)[-1]
    if not name or ".." in name or "\\" in name:
        return None
    if name in _MAPPINGS:
        return _MAPPINGS[name]
    try:
        return json.loads((config.captures_dir() / (name + ".map.json")).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_capture(uri: str):
    name = uri.rsplit("/", 1)[-1]
    if not name or "/" in name or "\\" in name or ".." in name:
        return None
    path = config.captures_dir() / name
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return data, "image/jpeg" if path.suffix.lower() in {".jpg", ".jpeg"} else "image/png"
