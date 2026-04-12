"""
ui_blueprint.extractor
======================
Converts an Android screen-recording MP4 (or synthetic frames) into a
structured Blueprint JSON that conforms to schema/blueprint.schema.json (v1).

The current implementation provides:
- optional real frame sampling via imageio/ffmpeg
- deterministic classical-CV element detection heuristics
- stable element tracking via IoU + appearance similarity
- basic motion fitting (step / linear / bezier / sampled)
- heuristic event inference for scroll and tap-like state changes
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
import zlib
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps, ImageStat

try:
    import imageio.v2 as imageio
except ImportError:  # pragma: no cover - optional dependency path
    imageio = None

SCHEMA_VERSION = "1.0"
DEFAULT_CHUNK_MS = 1000
DEFAULT_SAMPLE_FPS = 10
_MIN_COMPONENT_AREA = 16
_BG_DIFF_THRESHOLD = 18
_EDGE_THRESHOLD = 32
_TEXT_DARK_THRESHOLD = 55
_MERGE_GAP_PX = 18
_IOU_WEIGHT = 0.7
_APPEARANCE_WEIGHT = 0.3
_TRACK_MATCH_THRESHOLD = 0.35
_LINEAR_RESIDUAL_THRESHOLD = 2.0
_BEZIER_RESIDUAL_THRESHOLD = 3.5
_SCROLL_EVENT_THRESHOLD = 12.0
_TAP_COLOR_THRESHOLD = 8.0
_FRAME_TIMESTAMP_TOLERANCE_MS = 0.5


def _build_synthetic_meta() -> dict[str, Any]:
    """Return synthetic metadata for testing without a real video file."""
    return {
        "width_px": 1080,
        "height_px": 1920,
        "fps": 30.0,
        "duration_ms": 10_000.0,
        "source_file": "synthetic",
        "device": "Synthetic/Android 14",
        "os_version": "14",
    }


def _generate_synthetic_frame(meta: dict[str, Any], ts_ms: float) -> Image.Image:
    """Generate a deterministic synthetic UI frame for tests and preview validation."""
    width = int(meta["width_px"])
    height = int(meta["height_px"])
    duration_ms = max(float(meta["duration_ms"]), 1.0)
    progress = ts_ms / duration_ms
    scroll_offset = progress * 180.0
    button_active = 4300.0 <= ts_ms <= 4700.0

    image = Image.new("RGB", (width, height), color=(245, 245, 245))
    draw = ImageDraw.Draw(image)

    app_margin_x = max(18, int(width * 0.08))
    app_top = max(28, int(height * 0.06))
    app_bottom = app_top + max(60, int(height * 0.06))
    draw.rounded_rectangle(
        (app_margin_x, app_top, width - app_margin_x, app_bottom),
        radius=max(12, int(width * 0.02)),
        fill=(255, 255, 255),
        outline=(210, 210, 210),
    )
    draw.text((app_margin_x + 20, app_top + 18), "Blueprint capture", fill=(30, 30, 30))

    button_fill = (70, 130, 180) if not button_active else (105, 164, 214)
    button_left = int(width * 0.25)
    button_right = int(width * 0.75)
    button_top = int(height * 0.40) + int(progress * max(4, height * 0.005))
    button_height = max(54, int(height * 0.065))
    draw.rounded_rectangle(
        (button_left, button_top, button_right, button_top + button_height),
        radius=max(16, int(width * 0.03)),
        fill=button_fill,
    )
    draw.text((button_left + 28, button_top + button_height // 3), "Continue", fill=(255, 255, 255))

    row_margin = max(18, int(width * 0.06))
    row_height = max(64, int(height * 0.07))
    row_gap = max(22, int(height * 0.02))
    base_y = int(height * 0.52) - scroll_offset
    for row_idx in range(5):
        top = int(base_y + row_idx * (row_height + row_gap))
        bottom = top + row_height
        if bottom < app_bottom + 24 or top > height - 40:
            continue
        fill = (255, 255, 255) if row_idx % 2 == 0 else (250, 250, 250)
        draw.rounded_rectangle(
            (row_margin, top, width - row_margin, bottom),
            radius=max(14, int(width * 0.025)),
            fill=fill,
            outline=(220, 220, 220),
        )
        icon_size = max(20, int(row_height * 0.42))
        icon_left = row_margin + max(16, int(width * 0.03))
        icon_top = top + (row_height - icon_size) // 2
        draw.ellipse(
            (icon_left, icon_top, icon_left + icon_size, icon_top + icon_size),
            fill=(120, 170, 235),
        )
        text_left = icon_left + icon_size + max(18, int(width * 0.03))
        draw.text(
            (text_left, top + row_height * 0.23), f"Row item {row_idx + 1}", fill=(40, 40, 40)
        )
        draw.text(
            (text_left, top + row_height * 0.54),
            "Synthetic scrolling content",
            fill=(110, 110, 110),
        )

    return image


def _sample_synthetic_frames(meta: dict[str, Any], sample_fps: float) -> list[dict[str, Any]]:
    """Create synthetic frame samples at the requested analysis rate."""
    duration_ms = float(meta["duration_ms"])
    frame_interval_ms = 1000.0 / max(sample_fps, 1.0)
    samples: list[dict[str, Any]] = []
    timestamp_ms = 0.0
    while timestamp_ms <= duration_ms + 1e-6:
        image = _generate_synthetic_frame(meta, timestamp_ms)
        samples.append({"t_ms": round(timestamp_ms, 3), "image": image})
        timestamp_ms += frame_interval_ms
    return samples


def _read_mp4_metadata(path: Path) -> dict[str, Any]:
    """Parse basic metadata from an MP4 file without external dependencies."""
    meta: dict[str, Any] = {
        "width_px": 1080,
        "height_px": 1920,
        "fps": 30.0,
        "duration_ms": 10_000.0,
        "source_file": path.name,
    }
    try:
        data = path.read_bytes()
        offset = 0
        while offset + 8 <= len(data):
            size = struct.unpack_from(">I", data, offset)[0]
            box_type = data[offset + 4 : offset + 8]
            if size < 8:
                break
            if box_type == b"mvhd":
                version = data[offset + 8]
                if version == 0:
                    timescale = struct.unpack_from(">I", data, offset + 20)[0]
                    duration = struct.unpack_from(">I", data, offset + 24)[0]
                else:
                    timescale = struct.unpack_from(">I", data, offset + 28)[0]
                    duration = struct.unpack_from(">Q", data, offset + 32)[0]
                if timescale > 0:
                    meta["duration_ms"] = round(duration / timescale * 1000, 3)
                break
            if box_type in (b"moov", b"trak", b"mdia", b"minf", b"stbl"):
                offset += 8
                continue
            offset += size
    except Exception:  # noqa: BLE001
        pass
    return meta


def _sample_video_frames(
    video_path: Path,
    sample_fps: float,
    fallback_meta: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Decode frames via imageio when available, keeping metadata parsing fallback."""
    if imageio is None:
        return [], fallback_meta

    try:
        reader = imageio.get_reader(str(video_path))
        reader_meta = reader.get_meta_data()
        fps = float(reader_meta.get("fps") or fallback_meta.get("fps") or sample_fps)
        fps = fps if fps > 0 else sample_fps
        samples: list[dict[str, Any]] = []
        next_sample_ms = 0.0
        last_index = 0
        for frame_index, frame_array in enumerate(reader):
            timestamp_ms = frame_index / fps * 1000.0
            if timestamp_ms + _FRAME_TIMESTAMP_TOLERANCE_MS < next_sample_ms:
                continue
            image = Image.fromarray(frame_array).convert("RGB")
            if not samples:
                fallback_meta["width_px"], fallback_meta["height_px"] = image.size
            samples.append({"t_ms": round(timestamp_ms, 3), "image": image})
            next_sample_ms += 1000.0 / max(sample_fps, 1.0)
            last_index = frame_index
        reader.close()
        if samples:
            fallback_meta["fps"] = fps
            last_duration_ms = (
                last_index / fps * 1000.0 if fps > 0 else fallback_meta["duration_ms"]
            )
            fallback_meta["duration_ms"] = max(
                fallback_meta["duration_ms"], round(last_duration_ms, 3)
            )
        return samples, fallback_meta
    except Exception:  # noqa: BLE001
        return [], fallback_meta


def _connected_components(mask: Image.Image) -> list[tuple[int, int, int, int, int]]:
    """Return bounding boxes for connected components in a binary mask."""
    width, height = mask.size
    pixels = mask.load()
    visited = [[False for _ in range(width)] for _ in range(height)]
    boxes: list[tuple[int, int, int, int, int]] = []

    for x in range(width):
        for y in range(height):
            if visited[y][x] or pixels[x, y] == 0:
                continue
            stack = [(x, y)]
            visited[y][x] = True
            min_x = max_x = x
            min_y = max_y = y
            area = 0
            while stack:
                cx, cy = stack.pop()
                area += 1
                min_x = min(min_x, cx)
                max_x = max(max_x, cx)
                min_y = min(min_y, cy)
                max_y = max(max_y, cy)
                for nx, ny in ((cx - 1, cy), (cx + 1, cy), (cx, cy - 1), (cx, cy + 1)):
                    if 0 <= nx < width and 0 <= ny < height:
                        if not visited[ny][nx] and pixels[nx, ny] != 0:
                            visited[ny][nx] = True
                            stack.append((nx, ny))
            if area >= _MIN_COMPONENT_AREA:
                boxes.append((min_x, min_y, max_x, max_y, area))
    return boxes


def _merge_boxes(boxes: list[dict[str, float]], max_gap: float) -> list[dict[str, float]]:
    """Merge overlapping or near-touching boxes."""
    merged = boxes[:]
    changed = True
    while changed:
        changed = False
        next_boxes: list[dict[str, float]] = []
        while merged:
            current = merged.pop(0)
            cx0, cy0, cx1, cy1 = (
                current["x"],
                current["y"],
                current["x"] + current["w"],
                current["y"] + current["h"],
            )
            merge_indices: list[int] = []
            for index, candidate in enumerate(merged):
                dx0, dy0, dx1, dy1 = (
                    candidate["x"],
                    candidate["y"],
                    candidate["x"] + candidate["w"],
                    candidate["y"] + candidate["h"],
                )
                overlaps = not (
                    dx0 > cx1 + max_gap
                    or dx1 < cx0 - max_gap
                    or dy0 > cy1 + max_gap
                    or dy1 < cy0 - max_gap
                )
                if overlaps:
                    cx0 = min(cx0, dx0)
                    cy0 = min(cy0, dy0)
                    cx1 = max(cx1, dx1)
                    cy1 = max(cy1, dy1)
                    merge_indices.append(index)
                    changed = True
            for index in reversed(merge_indices):
                merged.pop(index)
            next_boxes.append({"x": cx0, "y": cy0, "w": cx1 - cx0, "h": cy1 - cy0})
        merged = next_boxes
    return merged


def _background_color(image: Image.Image) -> tuple[int, int, int]:
    """Estimate background color from the image corners."""
    width, height = image.size
    samples = [
        image.crop((0, 0, 24, 24)),
        image.crop((width - 24, 0, width, 24)),
        image.crop((0, height - 24, 24, height)),
        image.crop((width - 24, height - 24, width, height)),
    ]
    means = [ImageStat.Stat(sample).mean for sample in samples]
    return tuple(int(sum(values) / len(values)) for values in zip(*means))


def _compute_dark_text_cutoff(background_rgb: tuple[int, int, int]) -> int:
    """Compute a grayscale cutoff for likely dark text against the sampled background."""
    return max(0, sum(background_rgb) // 3 - _TEXT_DARK_THRESHOLD)


def _appearance_signature(image: Image.Image, bbox: dict[str, float]) -> dict[str, Any]:
    """Compute simple appearance features for matching and event inference."""
    x0 = max(0, int(bbox["x"]))
    y0 = max(0, int(bbox["y"]))
    x1 = min(image.width, int(bbox["x"] + bbox["w"]))
    y1 = min(image.height, int(bbox["y"] + bbox["h"]))
    crop = image.crop((x0, y0, x1, y1)) if x1 > x0 and y1 > y0 else Image.new("RGB", (1, 1))
    stat = ImageStat.Stat(crop)
    mean_rgb = tuple(round(value, 2) for value in stat.mean[:3])
    grayscale = ImageOps.grayscale(crop)
    edge_density = ImageStat.Stat(grayscale.filter(ImageFilter.FIND_EDGES)).mean[0] / 255.0
    return {"mean_rgb": mean_rgb, "edge_density": round(edge_density, 4)}


def _appearance_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Return a similarity score between two appearance signatures in [0, 1]."""
    left_rgb = left.get("appearance", {}).get("mean_rgb", (0.0, 0.0, 0.0))
    right_rgb = right.get("appearance", {}).get("mean_rgb", (0.0, 0.0, 0.0))
    distance = math.sqrt(sum((left_rgb[idx] - right_rgb[idx]) ** 2 for idx in range(3)))
    similarity = 1.0 - min(distance / 255.0, 1.0)
    edge_left = left.get("appearance", {}).get("edge_density", 0.0)
    edge_right = right.get("appearance", {}).get("edge_density", 0.0)
    edge_similarity = 1.0 - min(abs(edge_left - edge_right), 1.0)
    return max(0.0, min((similarity * 0.75) + (edge_similarity * 0.25), 1.0))


def _bbox_center(bbox: dict[str, float]) -> tuple[float, float]:
    return bbox["x"] + bbox["w"] / 2.0, bbox["y"] + bbox["h"] / 2.0


def _iou(left: dict[str, float], right: dict[str, float]) -> float:
    """Intersection-over-union for two boxes."""
    left_x1 = left["x"] + left["w"]
    left_y1 = left["y"] + left["h"]
    right_x1 = right["x"] + right["w"]
    right_y1 = right["y"] + right["h"]
    inter_x0 = max(left["x"], right["x"])
    inter_y0 = max(left["y"], right["y"])
    inter_x1 = min(left_x1, right_x1)
    inter_y1 = min(left_y1, right_y1)
    inter_w = max(0.0, inter_x1 - inter_x0)
    inter_h = max(0.0, inter_y1 - inter_y0)
    inter_area = inter_w * inter_h
    if inter_area <= 0:
        return 0.0
    left_area = left["w"] * left["h"]
    right_area = right["w"] * right["h"]
    union = left_area + right_area - inter_area
    return inter_area / union if union > 0 else 0.0


def _classify_detection(
    crop: Image.Image,
    bbox: dict[str, float],
    frame_width: int,
    frame_height: int,
) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Classify a detected region into a simple UI element type."""
    w = bbox["w"]
    h = bbox["h"]
    area_ratio = (w * h) / max(frame_width * frame_height, 1)
    aspect = w / max(h, 1.0)
    stat = ImageStat.Stat(crop)
    mean_rgb = tuple(int(value) for value in stat.mean[:3])
    variance = sum(stat.var[:3]) / 3.0 if stat.var else 0.0
    edge_density = (
        ImageStat.Stat(ImageOps.grayscale(crop).filter(ImageFilter.FIND_EDGES)).mean[0] / 255.0
    )

    y_center = (bbox["y"] + bbox["h"] / 2) / max(frame_height, 1)

    if area_ratio >= 0.80:
        element_type = "container"
    elif w <= frame_width * 0.14 and h <= frame_height * 0.10:
        element_type = "icon"
    elif aspect >= 2.0 and h <= frame_height * 0.10:
        element_type = "text" if edge_density >= 0.14 and variance < 1600 else "button"
    elif aspect >= 1.2 and h <= frame_height * 0.16:
        element_type = "list_item"
    elif h >= frame_height * 0.30 and aspect <= 0.9:
        element_type = "scroll_view"
    elif 0.04 <= area_ratio <= 0.08 and 0.7 <= aspect <= 1.4:
        element_type = "fab"
    elif y_center >= 0.75 and aspect >= 3.0 and h <= frame_height * 0.06:
        element_type = "bottom_sheet"
    elif y_center >= 0.85 and w >= frame_width * 0.85:
        element_type = "tab_bar"
    elif y_center <= 0.12 and w >= frame_width * 0.85:
        element_type = "toolbar"
    elif 0.60 <= y_center <= 0.85 and aspect >= 3.0 and area_ratio <= 0.12:
        element_type = "snackbar"
    elif 0.2 <= y_center <= 0.8 and 0.15 <= area_ratio <= 0.60 and 0.6 <= aspect <= 2.0:
        element_type = "dialog"
    elif aspect >= 3.0 and h <= frame_height * 0.08 and w <= frame_width * 0.92:
        element_type = "input_field"
    else:
        element_type = "unknown"

    style = {"bg_color": {"r": mean_rgb[0], "g": mean_rgb[1], "b": mean_rgb[2], "a": 255}}
    semantics = {
        "clickable": element_type in {"button", "list_item", "icon"},
        "scrollable": element_type == "scroll_view",
    }
    content: dict[str, Any] = {}
    text = _ocr_region(crop.tobytes(), bbox, crop.width, crop.height)
    if text:
        content["text"] = text
    return element_type, style, semantics, content


def _ocr_region(frame_rgb: bytes, bbox: dict[str, float], width: int, height: int) -> str:
    """OCR a region using pytesseract when available, otherwise return empty string."""
    try:
        import pytesseract  # type: ignore[import]

        image = Image.frombytes("RGB", (width, height), frame_rgb)
        return pytesseract.image_to_string(image).strip()
    except Exception:
        return ""


def _detect_elements(frame_rgb: bytes, width: int, height: int) -> list[dict[str, Any]]:
    """Detect coarse UI elements using classical image heuristics."""
    image = Image.frombytes("RGB", (width, height), frame_rgb)
    background_rgb = _background_color(image)
    background = Image.new("RGB", image.size, background_rgb)

    diff_mask = ImageChops.difference(image, background).convert("L")
    diff_mask = diff_mask.point(lambda value: 255 if value > _BG_DIFF_THRESHOLD else 0)
    edge_mask = ImageOps.grayscale(image).filter(ImageFilter.FIND_EDGES)
    edge_mask = edge_mask.point(lambda value: 255 if value > _EDGE_THRESHOLD else 0)
    dark_cutoff = _compute_dark_text_cutoff(background_rgb)
    dark_mask = ImageOps.grayscale(image).point(lambda value: 255 if value < dark_cutoff else 0)
    combined = ImageChops.lighter(ImageChops.lighter(diff_mask, edge_mask), dark_mask)
    combined = combined.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.MinFilter(3))

    scale = max(width / 180.0, height / 320.0, 1.0)
    reduced = combined.resize(
        (max(24, int(width / scale)), max(24, int(height / scale))), Image.Resampling.NEAREST
    )
    components = _connected_components(reduced)

    boxes: list[dict[str, float]] = []
    scale_x = width / reduced.width
    scale_y = height / reduced.height
    margin = max(4, int(scale * 2))
    for min_x, min_y, max_x, max_y, _area in components:
        x = max(0.0, min_x * scale_x - margin)
        y = max(0.0, min_y * scale_y - margin)
        w = min(width - x, (max_x - min_x + 1) * scale_x + margin * 2)
        h = min(height - y, (max_y - min_y + 1) * scale_y + margin * 2)
        if w * h < 800:
            continue
        boxes.append({"x": round(x, 2), "y": round(y, 2), "w": round(w, 2), "h": round(h, 2)})

    boxes = _merge_boxes(boxes, _MERGE_GAP_PX)
    detections: list[dict[str, Any]] = [
        {
            "type": "container",
            "bbox": {"x": 0.0, "y": 0.0, "w": float(width), "h": float(height)},
            "style": {
                "bg_color": {
                    "r": background_rgb[0],
                    "g": background_rgb[1],
                    "b": background_rgb[2],
                    "a": 255,
                }
            },
            "semantics": {"clickable": False, "scrollable": False},
            "content": {},
            "appearance": {
                "mean_rgb": tuple(float(channel) for channel in background_rgb),
                "edge_density": 0.0,
            },
        }
    ]

    for bbox in boxes:
        crop = image.crop((bbox["x"], bbox["y"], bbox["x"] + bbox["w"], bbox["y"] + bbox["h"]))
        element_type, style, semantics, content = _classify_detection(crop, bbox, width, height)
        detections.append(
            {
                "type": element_type,
                "bbox": bbox,
                "style": style,
                "semantics": semantics,
                "content": content,
                "appearance": _appearance_signature(image, bbox),
            }
        )

    return detections


def _fit_track_curve(timestamps_ms: list[float], values: list[float]) -> dict[str, Any]:
    """Fit step, linear, bezier, or sampled models to a 1D signal."""
    if not timestamps_ms:
        return {"model": "step", "params": {}, "keyframes": [], "residual_error": 0.0}

    if len(values) == 1 or max(values) - min(values) < 0.75:
        return {
            "model": "step",
            "params": {"value": round(values[0], 4)},
            "keyframes": [{"t_ms": timestamps_ms[0], "value": values[0]}],
            "residual_error": 0.0,
        }

    start_t = timestamps_ms[0]
    end_t = timestamps_ms[-1]
    duration = max(end_t - start_t, 1.0)
    slope = (values[-1] - values[0]) / duration
    intercept = values[0] - slope * start_t
    linear_predictions = [slope * timestamp + intercept for timestamp in timestamps_ms]
    linear_residual = sum(
        abs(prediction - value) for prediction, value in zip(linear_predictions, values)
    ) / len(values)
    if linear_residual <= _LINEAR_RESIDUAL_THRESHOLD:
        return {
            "model": "linear",
            "params": {"slope": round(slope, 6), "intercept": round(intercept, 6)},
            "residual_error": round(linear_residual, 4),
        }

    start_value = values[0]
    delta = values[-1] - start_value
    if abs(delta) > 1e-6:
        normalized_targets = [(value - start_value) / delta for value in values]
        normalized_times = [(timestamp - start_t) / duration for timestamp in timestamps_ms]
        best: dict[str, Any] | None = None
        for c1 in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
            for c2 in (0.0, 0.2, 0.4, 0.6, 0.8, 1.0):
                predictions = [
                    (3 * ((1 - t) ** 2) * t * c1) + (3 * (1 - t) * (t**2) * c2) + (t**3)
                    for t in normalized_times
                ]
                residual = sum(
                    abs(prediction - target)
                    for prediction, target in zip(predictions, normalized_targets)
                ) / len(values)
                if best is None or residual < best["residual"]:
                    best = {"c1": c1, "c2": c2, "residual": residual}
        if best is not None and (best["residual"] * abs(delta)) <= _BEZIER_RESIDUAL_THRESHOLD:
            return {
                "model": "bezier",
                "params": {
                    "start_value": round(start_value, 6),
                    "end_value": round(values[-1], 6),
                    "control_y1": best["c1"],
                    "control_y2": best["c2"],
                },
                "residual_error": round(best["residual"] * abs(delta), 4),
            }

    return {
        "model": "sampled",
        "params": {},
        "keyframes": [{"t_ms": t_ms, "value": value} for t_ms, value in zip(timestamps_ms, values)],
        "residual_error": 0.0,
    }


def _track_elements(
    prev_elements: list[dict[str, Any]],
    curr_elements: list[dict[str, Any]],
    *,
    next_element_index: int,
) -> tuple[list[dict[str, Any]], int]:
    """Assign stable IDs with IoU + appearance similarity matching."""
    tracked: list[dict[str, Any]] = []
    unmatched_prev = {element["id"]: element for element in prev_elements if "id" in element}

    for element in curr_elements:
        best_prev_id: str | None = None
        best_score = -1.0
        for prev_id, prev_element in unmatched_prev.items():
            if prev_element.get("type") != element.get("type"):
                continue
            iou_score = _iou(prev_element["bbox"], element["bbox"])
            appearance_score = _appearance_similarity(prev_element, element)
            score = (_IOU_WEIGHT * iou_score) + (_APPEARANCE_WEIGHT * appearance_score)
            if score > best_score:
                best_score = score
                best_prev_id = prev_id
        if best_prev_id is not None and best_score >= _TRACK_MATCH_THRESHOLD:
            element["id"] = best_prev_id
            unmatched_prev.pop(best_prev_id)
        else:
            element["id"] = f"el_{next_element_index:04d}"
            next_element_index += 1
        tracked.append(element)

    return tracked, next_element_index


def _appearance_delta(left: dict[str, Any], right: dict[str, Any]) -> float:
    """Compute appearance delta between two tracked elements."""
    left_rgb = left.get("appearance", {}).get("mean_rgb", (0.0, 0.0, 0.0))
    right_rgb = right.get("appearance", {}).get("mean_rgb", (0.0, 0.0, 0.0))
    return sum(abs(left_rgb[idx] - right_rgb[idx]) for idx in range(3)) / 3.0


def _infer_events(
    chunk_elements: list[list[dict[str, Any]]],
    sample_timestamps_ms: list[float],
    frame_width: int,
    frame_height: int,
) -> list[dict[str, Any]]:
    """Infer coarse scroll and tap-like events from tracked element motion."""
    if len(chunk_elements) < 2:
        return []

    events: list[dict[str, Any]] = []
    first_map = {
        element["id"]: element
        for element in chunk_elements[0]
        if element.get("type") not in {"container", "unknown"}
    }
    last_map = {
        element["id"]: element
        for element in chunk_elements[-1]
        if element.get("type") not in {"container", "unknown"}
    }
    shared_ids = [element_id for element_id in first_map if element_id in last_map]
    vertical_deltas = [
        _bbox_center(last_map[element_id]["bbox"])[1]
        - _bbox_center(first_map[element_id]["bbox"])[1]
        for element_id in shared_ids
    ]
    if len(vertical_deltas) < 2:
        first_positions = sorted(
            [
                _bbox_center(element["bbox"])[1]
                for element in chunk_elements[0]
                if element.get("type") not in {"container", "unknown"}
            ]
        )
        last_positions = sorted(
            [
                _bbox_center(element["bbox"])[1]
                for element in chunk_elements[-1]
                if element.get("type") not in {"container", "unknown"}
            ]
        )
        pair_count = min(len(first_positions), len(last_positions))
        if pair_count >= 2:
            vertical_deltas = [
                last_positions[index] - first_positions[index] for index in range(pair_count)
            ]
    if len(vertical_deltas) >= 2:
        median_dy = median(vertical_deltas)
        same_direction = [
            delta for delta in vertical_deltas if delta == 0 or (delta > 0) == (median_dy > 0)
        ]
        if abs(median_dy) >= _SCROLL_EVENT_THRESHOLD and len(same_direction) >= max(
            2, len(vertical_deltas) // 2
        ):
            events.append(
                {
                    "t_ms": round((sample_timestamps_ms[0] + sample_timestamps_ms[-1]) / 2.0, 3),
                    "kind": "scroll",
                    "target": {"x": frame_width / 2.0, "y": frame_height / 2.0},
                    "data": {
                        "delta_y": round(median_dy, 3),
                        "direction": "down" if median_dy > 0 else "up",
                    },
                    "confidence": 0.72,
                }
            )

    tap_emitted = False
    for index in range(1, len(chunk_elements)):
        prev_map = {
            element["id"]: element for element in chunk_elements[index - 1] if "id" in element
        }
        curr_map = {element["id"]: element for element in chunk_elements[index] if "id" in element}

        # Dismiss event: bbox present in frame N-1 disappears in frame N.
        for element_id, prev_element in prev_map.items():
            if element_id not in curr_map and prev_element.get("type") not in {
                "container",
                "unknown",
            }:
                prev_center = _bbox_center(prev_element["bbox"])
                mid_ms = (sample_timestamps_ms[index - 1] + sample_timestamps_ms[index]) / 2.0
                events.append(
                    {
                        "t_ms": round(mid_ms, 3),
                        "kind": "dismiss",
                        "target": {
                            "element_id": element_id,
                            "x": round(prev_center[0], 3),
                            "y": round(prev_center[1], 3),
                        },
                        "data": {"reason": "element_disappeared"},
                        "confidence": 0.55,
                    }
                )

        # Appear event: new large bbox in frame N not present in N-1.
        for element_id, curr_element in curr_map.items():
            if element_id not in prev_map and curr_element.get("type") not in {
                "container",
                "unknown",
            }:
                bbox = curr_element["bbox"]
                area_ratio = (bbox["w"] * bbox["h"]) / max(
                    frame_width * frame_height, 1
                )
                if area_ratio > 0.1:
                    curr_center = _bbox_center(bbox)
                    events.append(
                        {
                            "t_ms": sample_timestamps_ms[index],
                            "kind": "appear",
                            "target": {
                                "element_id": element_id,
                                "x": round(curr_center[0], 3),
                                "y": round(curr_center[1], 3),
                            },
                            "data": {
                                    "reason": "element_appeared",
                                    "area_ratio": round(area_ratio, 4),
                                },
                            "confidence": 0.55,
                        }
                    )

        for element_id in prev_map.keys() & curr_map.keys():
            prev_element = prev_map[element_id]
            curr_element = curr_map[element_id]
            if curr_element.get("type") not in {"button", "list_item", "icon"}:
                continue
            prev_center = _bbox_center(prev_element["bbox"])
            curr_center = _bbox_center(curr_element["bbox"])
            movement = math.dist(prev_center, curr_center)
            appearance_delta = _appearance_delta(prev_element, curr_element)
            if (
                movement <= max(8.0, frame_height * 0.004)
                and appearance_delta >= _TAP_COLOR_THRESHOLD
            ):
                events.append(
                    {
                        "t_ms": sample_timestamps_ms[index],
                        "kind": "tap",
                        "target": {
                            "element_id": element_id,
                            "x": round(curr_center[0], 3),
                            "y": round(curr_center[1], 3),
                        },
                        "data": {"reason": "appearance_change_without_motion"},
                        "confidence": 0.58,
                    }
                )
                tap_emitted = True
                break
        if tap_emitted:
            break

    return events


def _asset_id(index: int) -> str:
    return f"asset_{index:04d}"


def _content_hash(data: str) -> str:
    """Return a short deterministic content hash (not a perceptual hash)."""
    digest = zlib.adler32(data.encode()) & 0xFFFFFFFF
    return hashlib.blake2b(struct.pack(">I", digest), digest_size=6).hexdigest()


def _clean_catalog_entry(
    element: dict[str, Any], first_ms: float, last_ms: float
) -> dict[str, Any]:
    """Convert an internal tracked element to schema-compatible catalog entry."""
    entry: dict[str, Any] = {
        "id": element["id"],
        "type": element.get("type", "unknown"),
        "first_ms": round(first_ms, 3),
        "last_ms": round(last_ms, 3),
    }
    if style := element.get("style"):
        entry["style"] = style
    if content := element.get("content"):
        entry["content"] = content
    if semantics := element.get("semantics"):
        entry["semantics"] = semantics
    return entry


def _export_asset_crops(
    frames: list[dict[str, Any]],
    frame_sequences: dict[float, list[dict[str, Any]]],
    assets_dir: Path | None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Export one representative crop per tracked element when requested."""
    if assets_dir is None:
        return [], {}

    assets_dir.mkdir(parents=True, exist_ok=True)
    assets: list[dict[str, Any]] = []
    asset_map: dict[str, str] = {}
    frames_by_timestamp = {frame["t_ms"]: frame["image"] for frame in frames}
    exported: set[str] = set()

    for timestamp in sorted(frame_sequences):
        image = frames_by_timestamp.get(timestamp)
        if image is None:
            continue
        for element in frame_sequences[timestamp]:
            element_id = element["id"]
            if element_id in exported or element.get("type") == "container":
                continue
            bbox = element["bbox"]
            x0 = max(0, int(bbox["x"]))
            y0 = max(0, int(bbox["y"]))
            x1 = min(image.width, int(bbox["x"] + bbox["w"]))
            y1 = min(image.height, int(bbox["y"] + bbox["h"]))
            if x1 <= x0 or y1 <= y0:
                continue
            crop = image.crop((x0, y0, x1, y1))
            asset_id = _asset_id(len(assets))
            relative_path = f"{element_id}.png"
            crop.save(assets_dir / relative_path, "PNG")
            asset_map[element_id] = asset_id
            assets.append(
                {
                    "id": asset_id,
                    "kind": "screenshot_crop",
                    "path": str(assets_dir / relative_path),
                    "phash": _content_hash(f"{element_id}:{timestamp}"),
                    "bbox": bbox,
                    "frame_ms": timestamp,
                }
            )
            exported.add(element_id)
    return assets, asset_map


def extract(
    video_path: Path | None,
    *,
    synthetic: bool = False,
    chunk_ms: float = DEFAULT_CHUNK_MS,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    assets_dir: Path | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Extract a Blueprint from a video file or synthetic frame stream."""
    if not synthetic and video_path is None:
        raise ValueError("Either provide video_path or set synthetic=True.")

    meta = _build_synthetic_meta() if synthetic else _read_mp4_metadata(video_path or Path(""))
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    meta["created_at"] = created_at

    if synthetic:
        frames = _sample_synthetic_frames(meta, sample_fps)
    else:
        frames, meta = _sample_video_frames(video_path or Path(""), sample_fps, meta)
        if not frames:
            frames = [
                {
                    "t_ms": 0.0,
                    "image": Image.new(
                        "RGB",
                        (int(meta["width_px"]), int(meta["height_px"])),
                        color=(245, 245, 245),
                    ),
                }
            ]

    width = int(meta["width_px"])
    height = int(meta["height_px"])
    duration_ms = float(meta["duration_ms"])
    if frames:
        duration_ms = max(duration_ms, float(frames[-1]["t_ms"]))
        meta["duration_ms"] = duration_ms

    frame_sequences: dict[float, list[dict[str, Any]]] = {}
    previous_elements: list[dict[str, Any]] = []
    next_element_index = 0
    for frame in frames:
        image = frame["image"]
        detections = _detect_elements(image.tobytes(), image.width, image.height)
        tracked, next_element_index = _track_elements(
            previous_elements,
            detections,
            next_element_index=next_element_index,
        )
        frame_sequences[frame["t_ms"]] = tracked
        previous_elements = tracked

    asset_entries, asset_map = _export_asset_crops(frames, frame_sequences, assets_dir)

    catalog_by_id: dict[str, dict[str, Any]] = {}
    first_last_by_id: dict[str, tuple[float, float]] = {}
    for timestamp, elements in frame_sequences.items():
        for element in elements:
            element_id = element["id"]
            if element_id in first_last_by_id:
                first_seen, _last_seen = first_last_by_id[element_id]
                first_last_by_id[element_id] = (first_seen, timestamp)
            else:
                first_last_by_id[element_id] = (timestamp, timestamp)
                catalog_by_id[element_id] = element.copy()

    for element_id, asset_id in asset_map.items():
        content = catalog_by_id[element_id].setdefault("content", {})
        content.setdefault("asset_id", asset_id)

    elements_catalog = [
        _clean_catalog_entry(catalog_by_id[element_id], *first_last_by_id[element_id])
        for element_id in sorted(catalog_by_id)
    ]

    sample_timestamps = sorted(frame_sequences)
    chunks: list[dict[str, Any]] = []
    chunk_start = 0.0
    while chunk_start < max(duration_ms, chunk_ms):
        chunk_end = min(chunk_start + chunk_ms, duration_ms) if duration_ms > 0 else chunk_ms
        if chunk_end <= chunk_start:
            chunk_end = chunk_start + chunk_ms
        chunk_timestamps = [
            timestamp
            for timestamp in sample_timestamps
            if chunk_start <= timestamp <= chunk_end + 1e-6
        ]
        if not chunk_timestamps and sample_timestamps:
            chunk_timestamps = [min(sample_timestamps, key=lambda value: abs(value - chunk_start))]

        key_scene_elements = (
            frame_sequences.get(chunk_timestamps[0], []) if chunk_timestamps else []
        )
        key_scene = [
            {"element_id": element["id"], "bbox": element["bbox"], "z": index, "opacity": 1.0}
            for index, element in enumerate(key_scene_elements)
        ]

        tracks: list[dict[str, Any]] = []
        chunk_element_ids = {
            element["id"]
            for timestamp in chunk_timestamps
            for element in frame_sequences.get(timestamp, [])
        }
        for element_id in sorted(chunk_element_ids):
            for prop in ("translate_x", "translate_y", "width", "height", "opacity"):
                timestamps: list[float] = []
                values: list[float] = []
                for timestamp in chunk_timestamps:
                    for element in frame_sequences.get(timestamp, []):
                        if element["id"] != element_id:
                            continue
                        timestamps.append(round(timestamp - chunk_start, 3))
                        bbox = element["bbox"]
                        if prop == "translate_x":
                            values.append(float(bbox["x"]))
                        elif prop == "translate_y":
                            values.append(float(bbox["y"]))
                        elif prop == "width":
                            values.append(float(bbox["w"]))
                        elif prop == "height":
                            values.append(float(bbox["h"]))
                        else:
                            values.append(1.0)
                if not timestamps:
                    continue
                fitted = _fit_track_curve(timestamps, values)
                track: dict[str, Any] = {
                    "element_id": element_id,
                    "property": prop,
                    "model": fitted["model"],
                    "params": fitted["params"],
                    "residual_error": fitted.get("residual_error", 0.0),
                }
                if fitted.get("keyframes"):
                    track["keyframes"] = fitted["keyframes"]
                tracks.append(track)

        chunk_elements = [frame_sequences[timestamp] for timestamp in chunk_timestamps]
        events = _infer_events(chunk_elements, chunk_timestamps, width, height)
        non_container_counts = [
            sum(1 for element in frame_sequences[timestamp] if element.get("type") != "container")
            for timestamp in chunk_timestamps
        ]
        detection_confidence = min(
            1.0, (sum(non_container_counts) / max(len(non_container_counts), 1)) / 4.0
        )
        tracked_element_ids = {track["element_id"] for track in tracks}
        tracking_confidence = min(
            1.0,
            len(tracked_element_ids) / max(len(chunk_element_ids), 1),
        )
        chunks.append(
            {
                "t0_ms": round(chunk_start, 3),
                "t1_ms": round(min(chunk_end, duration_ms), 3),
                "key_scene": key_scene,
                "tracks": tracks,
                "events": events,
                "quality": {
                    "detection_confidence": round(detection_confidence, 3),
                    "tracking_confidence": round(tracking_confidence, 3),
                    "ocr_confidence": 0.0,
                },
            }
        )
        if chunk_end >= duration_ms:
            break
        chunk_start = chunk_end

    return {
        "version": SCHEMA_VERSION,
        "meta": meta,
        "assets": asset_entries,
        "elements_catalog": elements_catalog,
        "chunks": chunks,
    }


def save_blueprint(blueprint: dict[str, Any], output_path: Path) -> None:
    """Serialise blueprint dict to a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        json.dump(blueprint, handle, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Public segment-level extraction helpers
# ---------------------------------------------------------------------------

_EMPTY_SEGMENT_RESULT: dict[str, Any] = {
    "elements_catalog": [],
    "chunks": [],
    "events": [],
    "quality": {},
}


def _ffmpeg_exe() -> str:
    """Return the ffmpeg executable path."""
    try:
        import imageio_ffmpeg  # type: ignore[import]

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return "ffmpeg"


def extract_segment(clip_path: str, t0_ms: int, t1_ms: int) -> dict[str, Any]:
    """
    Run the full extraction pipeline on [t0_ms, t1_ms) of clip_path.

    Returns a dict with keys: elements_catalog, chunks, events, quality.
    Falls back to an empty result dict on any error.
    """
    import os
    import subprocess
    import tempfile

    try:
        ffmpeg = _ffmpeg_exe()
        start_s = t0_ms / 1000.0
        duration_s = max((t1_ms - t0_ms) / 1000.0, 0.1)

        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            segment_path = tmp.name

        try:
            cmd = [
                ffmpeg,
                "-ss", str(start_s),
                "-i", clip_path,
                "-t", str(duration_s),
                "-c", "copy",
                "-y",
                segment_path,
            ]
            subprocess.run(cmd, capture_output=True, timeout=120)

            if os.path.exists(segment_path) and os.path.getsize(segment_path) > 0:
                result = extract(Path(segment_path))
                all_events: list[dict[str, Any]] = [
                    event for chunk in result.get("chunks", []) for event in chunk.get("events", [])
                ]
                last_quality = (
                    result["chunks"][-1].get("quality", {}) if result.get("chunks") else {}
                )
                return {
                    "elements_catalog": result.get("elements_catalog", []),
                    "chunks": result.get("chunks", []),
                    "events": all_events,
                    "quality": last_quality,
                }
        finally:
            try:
                os.unlink(segment_path)
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass

    return dict(_EMPTY_SEGMENT_RESULT)


def extract_keyframes(clip_path: str, t0_ms: int, t1_ms: int) -> dict[str, Any]:
    """
    Extract representative keyframes for [t0_ms, t1_ms) of clip_path.

    Returns a dict with key ``frames``: a list of dicts with
    ``t_ms``, ``width``, and ``height`` fields.
    Falls back to ``{"frames": []}`` on any error.
    """
    import os
    import subprocess
    import tempfile

    try:
        ffmpeg = _ffmpeg_exe()
        start_s = t0_ms / 1000.0
        duration_s = max((t1_ms - t0_ms) / 1000.0, 0.1)

        with tempfile.TemporaryDirectory() as tmpdir:
            pattern = os.path.join(tmpdir, "kf_%04d.jpg")
            cmd = [
                ffmpeg,
                "-ss", str(start_s),
                "-i", clip_path,
                "-t", str(duration_s),
                "-r", "1",
                "-q:v", "3",
                "-y",
                pattern,
            ]
            subprocess.run(cmd, capture_output=True, timeout=120)

            frames: list[dict[str, Any]] = []
            for fname in sorted(os.listdir(tmpdir)):
                if not fname.endswith(".jpg"):
                    continue
                fpath = os.path.join(tmpdir, fname)
                try:
                    img = Image.open(fpath)
                    idx = int(fname.split("_")[1].split(".")[0]) - 1
                    frames.append(
                        {
                            "t_ms": round(t0_ms + idx * 1000.0, 3),
                            "width": img.width,
                            "height": img.height,
                        }
                    )
                except Exception:  # noqa: BLE001
                    pass

            return {"frames": frames}
    except Exception:  # noqa: BLE001
        return {"frames": []}


def extract_ocr(clip_path: str, t0_ms: int, t1_ms: int) -> dict[str, Any]:
    """
    Run OCR on sampled frames for [t0_ms, t1_ms) of clip_path.

    Returns a dict with key ``text_blocks``: a list of dicts with
    ``t_ms`` and ``text`` fields.
    Falls back to ``{"text_blocks": []}`` on any error.
    """
    import os
    import subprocess
    import tempfile

    try:
        pytesseract = None
        try:
            import pytesseract as _pytesseract  # type: ignore[import]

            pytesseract = _pytesseract
        except Exception:  # noqa: BLE001
            pass

        if pytesseract is None:
            return {"text_blocks": []}

        ffmpeg = _ffmpeg_exe()
        start_s = t0_ms / 1000.0
        duration_s = max((t1_ms - t0_ms) / 1000.0, 0.1)

        with tempfile.TemporaryDirectory() as tmpdir:
            pattern = os.path.join(tmpdir, "ocr_%04d.jpg")
            cmd = [
                ffmpeg,
                "-ss", str(start_s),
                "-i", clip_path,
                "-t", str(duration_s),
                "-r", "1",
                "-q:v", "3",
                "-y",
                pattern,
            ]
            subprocess.run(cmd, capture_output=True, timeout=120)

            text_blocks: list[dict[str, Any]] = []
            for fname in sorted(os.listdir(tmpdir)):
                if not fname.endswith(".jpg"):
                    continue
                fpath = os.path.join(tmpdir, fname)
                try:
                    img = Image.open(fpath)
                    text = pytesseract.image_to_string(img).strip()
                    idx = int(fname.split("_")[1].split(".")[0]) - 1
                    if text:
                        text_blocks.append(
                            {
                                "t_ms": round(t0_ms + idx * 1000.0, 3),
                                "text": text,
                            }
                        )
                except Exception:  # noqa: BLE001
                    pass

            return {"text_blocks": text_blocks}
    except Exception:  # noqa: BLE001
        return {"text_blocks": []}


def extract_transcript(clip_path: str, t0_ms: int, t1_ms: int) -> dict[str, Any]:
    """
    Extract audio transcript for [t0_ms, t1_ms) of clip_path.

    Returns a dict with key ``transcript``.  Currently returns an empty
    transcript; a real implementation would call a speech-to-text backend.
    Falls back gracefully on any error.
    """
    return {"transcript": ""}
