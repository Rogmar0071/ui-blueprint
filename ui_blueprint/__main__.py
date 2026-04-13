"""
ui_blueprint.__main__
=====================
Command-line entry point for the ui_blueprint package.

Sub-commands
------------
extract
    Convert an MP4 file (or synthetic data) into a blueprint JSON.

    Examples::

        python -m ui_blueprint extract recording.mp4 -o blueprint.json
        python -m ui_blueprint extract --synthetic -o blueprint.json

preview
    Render a directory of PNG preview frames from a blueprint JSON.

    Example::

        python -m ui_blueprint preview blueprint.json --out preview_frames/
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _cmd_extract(args: argparse.Namespace) -> int:
    from ui_blueprint.extractor import extract, save_blueprint

    video_path: Path | None = None
    if not args.synthetic:
        if args.video is None:
            print("error: provide a video path or use --synthetic", file=sys.stderr)
            return 2
        video_path = Path(args.video)
        if not video_path.exists():
            print(f"error: video file not found: {video_path}", file=sys.stderr)
            return 1

    assets_dir: Path | None = None
    if args.assets_dir:
        assets_dir = Path(args.assets_dir)

    blueprint = extract(
        video_path,
        synthetic=args.synthetic,
        chunk_ms=float(args.chunk_ms),
        sample_fps=float(args.sample_fps),
        assets_dir=assets_dir,
    )

    output_path = Path(args.output)
    save_blueprint(blueprint, output_path)
    print(f"Blueprint written to: {output_path}")
    return 0


def _cmd_preview(args: argparse.Namespace) -> int:
    from ui_blueprint.preview import render_preview

    blueprint_path = Path(args.blueprint)
    if not blueprint_path.exists():
        print(f"error: blueprint file not found: {blueprint_path}", file=sys.stderr)
        return 1

    output_dir = Path(args.out)
    written = render_preview(blueprint_path, output_dir)
    print(f"Preview frames written to: {output_dir}  ({len(written)} files)")
    return 0


def _cmd_split_analyze(args: argparse.Namespace) -> int:
    import json as _json

    from ui_blueprint.extractor import split_and_analyze

    video_path = Path(args.clip)
    if not video_path.exists():
        print(f"error: clip file not found: {video_path}", file=sys.stderr)
        return 1

    result = split_and_analyze(
        str(video_path),
        video_out=args.video_out or None,
        audio_out=args.audio_out or None,
    )

    ui_out = Path(args.ui_output)
    ui_out.parent.mkdir(parents=True, exist_ok=True)
    with ui_out.open("w", encoding="utf-8") as fh:
        _json.dump(result["ui_structure"], fh, indent=2, ensure_ascii=False)
    print(f"UI structure written to: {ui_out}")

    audio_out = Path(args.audio_output)
    audio_out.parent.mkdir(parents=True, exist_ok=True)
    with audio_out.open("w", encoding="utf-8") as fh:
        _json.dump(result["audio_transcript"], fh, indent=2, ensure_ascii=False)
    print(f"Audio transcript written to: {audio_out}")

    combined_out = Path(args.combined_output)
    combined_out.parent.mkdir(parents=True, exist_ok=True)
    with combined_out.open("w", encoding="utf-8") as fh:
        _json.dump(result, fh, indent=2, ensure_ascii=False)
    print(f"Combined analysis written to: {combined_out}")

    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m ui_blueprint",
        description="UI Blueprint tools — extract blueprints from video and render previews.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # --- extract -------------------------------------------------------------
    p_extract = sub.add_parser(
        "extract",
        help="Extract a blueprint JSON from an MP4 or synthetic data.",
    )
    p_extract.add_argument(
        "video",
        nargs="?",
        default=None,
        metavar="VIDEO",
        help="Path to source MP4 file (omit when using --synthetic).",
    )
    p_extract.add_argument(
        "-o", "--output",
        required=True,
        metavar="OUT_JSON",
        help="Output path for the blueprint JSON file.",
    )
    p_extract.add_argument(
        "--synthetic",
        action="store_true",
        help="Generate blueprint from synthetic metadata (no real video needed).",
    )
    p_extract.add_argument(
        "--chunk-ms",
        dest="chunk_ms",
        type=float,
        default=1000,
        metavar="MS",
        help="Chunk duration in milliseconds (default: 1000).",
    )
    p_extract.add_argument(
        "--sample-fps",
        dest="sample_fps",
        type=float,
        default=10,
        metavar="FPS",
        help="Frame sampling rate for analysis (default: 10).",
    )
    p_extract.add_argument(
        "--assets-dir",
        dest="assets_dir",
        default=None,
        metavar="DIR",
        help="If provided, create an asset-crops directory and record paths.",
    )
    p_extract.set_defaults(func=_cmd_extract)

    # --- preview -------------------------------------------------------------
    p_preview = sub.add_parser(
        "preview",
        help="Render PNG preview frames from a blueprint JSON.",
    )
    p_preview.add_argument(
        "blueprint",
        metavar="BLUEPRINT_JSON",
        help="Path to blueprint JSON file.",
    )
    p_preview.add_argument(
        "--out",
        required=True,
        metavar="OUT_DIR",
        help="Output directory for PNG preview frames.",
    )
    p_preview.set_defaults(func=_cmd_preview)

    # --- split-analyze -------------------------------------------------------
    p_split = sub.add_parser(
        "split-analyze",
        help=(
            "Split a media clip into video and audio tracks, analyze each "
            "separately, and write combined results."
        ),
    )
    p_split.add_argument(
        "clip",
        metavar="CLIP",
        help="Path to the source media file (e.g. clip.mp4).",
    )
    p_split.add_argument(
        "--video-out",
        dest="video_out",
        default=None,
        metavar="VIDEO_FILE",
        help=(
            "Where to save the video-only track (e.g. analysis/video_only.mp4). "
            "A temporary file is used when omitted."
        ),
    )
    p_split.add_argument(
        "--audio-out",
        dest="audio_out",
        default=None,
        metavar="AUDIO_FILE",
        help=(
            "Where to save the audio-only track (e.g. analysis/audio_only.wav). "
            "A temporary file is used when omitted."
        ),
    )
    p_split.add_argument(
        "--ui-output",
        dest="ui_output",
        default="results/ui_structure.json",
        metavar="UI_JSON",
        help="Output path for the UI structure JSON (default: results/ui_structure.json).",
    )
    p_split.add_argument(
        "--audio-output",
        dest="audio_output",
        default="results/audio_transcript.json",
        metavar="AUDIO_JSON",
        help=(
            "Output path for the audio transcript JSON "
            "(default: results/audio_transcript.json)."
        ),
    )
    p_split.add_argument(
        "--combined-output",
        dest="combined_output",
        default="results/combined_analysis.json",
        metavar="COMBINED_JSON",
        help=(
            "Output path for the combined analysis JSON "
            "(default: results/combined_analysis.json)."
        ),
    )
    p_split.set_defaults(func=_cmd_split_analyze)

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
