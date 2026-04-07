"""
ui_blueprint.preview
====================
Reads a Blueprint JSON file and renders a visual preview of the timeline by
drawing bounding boxes and text labels onto blank frames, one PNG per
chunk keyframe.

Usage (CLI):
    python -m ui_blueprint preview out.json --out preview_frames/
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageDraw, ImageFont

    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# Colour palette for element types
# ---------------------------------------------------------------------------
_TYPE_COLORS: dict[str, tuple[int, int, int]] = {
    "container": (220, 220, 220),
    "button": (70, 130, 180),
    "text": (60, 179, 113),
    "icon": (255, 165, 0),
    "list_item": (147, 112, 219),
    "scroll_view": (100, 149, 237),
    "input_field": (255, 215, 0),
    "keyboard": (169, 169, 169),
    "cursor": (255, 0, 0),
    "overlay": (128, 0, 128),
    "unknown": (160, 160, 160),
}

_DEFAULT_COLOR = (160, 160, 160)
_BG_COLOR = (245, 245, 245)
_BORDER_WIDTH = 3
_MAX_PREVIEW_SIZE = (540, 960)  # downscale for readability
_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",  # Debian/Ubuntu
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",  # Fedora/RHEL
    "/Library/Fonts/Arial.ttf",  # macOS
    "C:\\Windows\\Fonts\\arial.ttf",  # Windows
]


def _load_blueprint(path: Path) -> dict[str, Any]:
    """Load and parse a blueprint JSON file."""
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _scale_factor(width_px: int, height_px: int) -> float:
    """Compute scale factor to fit the frame within _MAX_PREVIEW_SIZE."""
    sw = _MAX_PREVIEW_SIZE[0] / max(width_px, 1)
    sh = _MAX_PREVIEW_SIZE[1] / max(height_px, 1)
    return min(sw, sh, 1.0)


def _element_label(scene_node: dict[str, Any], catalog: dict[str, Any]) -> str:
    """Build a short label string for a scene node."""
    eid = scene_node.get("element_id", "?")
    el_def = catalog.get(eid, {})
    el_type = el_def.get("type", "unknown")
    text = (el_def.get("content") or {}).get("text", "")
    if text:
        short = text[:20] + ("…" if len(text) > 20 else "")
        return f"[{el_type}] {short}"
    return f"[{el_type}] {eid}"


def render_preview(
    blueprint_path: Path,
    output_dir: Path,
) -> list[Path]:
    """
    Render one PNG frame per blueprint chunk (keyframe scene).

    Args:
        blueprint_path: path to the blueprint JSON file.
        output_dir: directory to write PNG files into.

    Returns:
        List of paths to written PNG files.
    """
    if not _PIL_AVAILABLE:
        raise RuntimeError(
            "Pillow is required for preview rendering. Install it with: pip install Pillow"
        )

    blueprint = _load_blueprint(blueprint_path)
    meta = blueprint.get("meta", {})
    width_px: int = int(meta.get("width_px", 1080))
    height_px: int = int(meta.get("height_px", 1920))

    # Build element catalog lookup: id → element_def
    catalog: dict[str, Any] = {el["id"]: el for el in blueprint.get("elements_catalog", [])}

    scale = _scale_factor(width_px, height_px)
    canvas_w = max(1, round(width_px * scale))
    canvas_h = max(1, round(height_px * scale))

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    chunks: list[dict[str, Any]] = blueprint.get("chunks", [])
    for chunk_idx, chunk in enumerate(chunks):
        t0 = chunk.get("t0_ms", 0)
        t1 = chunk.get("t1_ms", 0)
        key_scene: list[dict[str, Any]] = chunk.get("key_scene", [])
        events: list[dict[str, Any]] = chunk.get("events", [])

        img = Image.new("RGB", (canvas_w, canvas_h), color=_BG_COLOR)
        draw = ImageDraw.Draw(img)

        # Try to load a basic font from common OS locations; fall back to Pillow's default.
        font = None
        small_font = None
        for _fp in _FONT_CANDIDATES:
            try:
                font = ImageFont.truetype(_fp, 14)
                small_font = ImageFont.truetype(_fp, 11)
                break
            except (IOError, OSError):
                continue
        if font is None:
            font = ImageFont.load_default()
            small_font = font

        # Sort by z-order.
        sorted_nodes = sorted(key_scene, key=lambda n: n.get("z", 0))

        for node in sorted_nodes:
            bbox_raw = node.get("bbox", {})
            x = round(bbox_raw.get("x", 0) * scale)
            y = round(bbox_raw.get("y", 0) * scale)
            w = max(1, round(bbox_raw.get("w", 10) * scale))
            h = max(1, round(bbox_raw.get("h", 10) * scale))
            opacity = node.get("opacity", 1.0)

            eid = node.get("element_id", "")
            el_def = catalog.get(eid, {})
            el_type = el_def.get("type", "unknown")
            fill_rgb = _TYPE_COLORS.get(el_type, _DEFAULT_COLOR)

            # Blend fill with background based on opacity.
            blended = tuple(
                round(fill_rgb[i] * opacity + _BG_COLOR[i] * (1 - opacity)) for i in range(3)
            )

            draw.rectangle(
                [x, y, x + w, y + h],
                fill=blended,
                outline=(80, 80, 80),
                width=_BORDER_WIDTH,
            )

            label = _element_label(node, catalog)
            label_x = x + _BORDER_WIDTH + 2
            label_y = y + _BORDER_WIDTH + 2
            if label_x < canvas_w and label_y < canvas_h:
                draw.text((label_x, label_y), label, fill=(20, 20, 20), font=font)

        # Draw event markers.
        for event in events:
            target = event.get("target") or {}
            ex = target.get("x")
            ey = target.get("y")
            if ex is not None and ey is not None:
                px, py = round(ex * scale), round(ey * scale)
                r = 8
                draw.ellipse([px - r, py - r, px + r, py + r], fill=(255, 50, 50))
                draw.text(
                    (px + r + 2, py - r),
                    event.get("kind", "?"),
                    fill=(200, 0, 0),
                    font=small_font,
                )

        # Draw timestamp header.
        header = (
            f"Chunk {chunk_idx:03d}  |  t={t0:.0f}–{t1:.0f} ms  "
            f"|  {len(key_scene)} elements  |  {len(events)} events"
        )
        draw.rectangle([0, 0, canvas_w, 22], fill=(30, 30, 30))
        draw.text((4, 4), header, fill=(240, 240, 240), font=small_font)

        out_path = output_dir / f"chunk_{chunk_idx:04d}.png"
        img.save(out_path, "PNG")
        written.append(out_path)

    return written
