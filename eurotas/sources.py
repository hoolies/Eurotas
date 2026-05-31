"""Source descriptors and ffmpeg command construction.

Each source is normalized into the same pipeline:
    [pre-input opts] -> [inputs] -> [maps] -> [scale/pad/fps] -> [H.264/AAC] -> [HLS]

Only ONE source is ever active at a time (it is a single channel), so at most
one ffmpeg encode runs.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass, field
from typing import Any

from .config import Config


# Source kinds that are expected to run forever (looping). The failover
# watchdog only treats a *stall* of the non-looping kinds as a failure.
LOOPING_KINDS = {"tutorial", "slideshow", "testpattern"}


@dataclass
class SourceDescriptor:
    kind: str
    title: str
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def looping(self) -> bool:
        if self.kind in LOOPING_KINDS:
            return True
        if self.kind == "file" and self.params.get("loop"):
            return True
        return False


class SourceError(Exception):
    pass


def _video_filter(cfg: Config) -> str:
    w, h, fps = cfg["width"], cfg["height"], cfg["fps"]
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p"
    )


def _video_encode_args(cfg: Config) -> list[str]:
    enc = cfg["video_encoder"]
    bv = cfg["video_bitrate"]
    bufsize = _double_bitrate(bv)
    fps = int(cfg["fps"])
    gop = fps * int(cfg["hls_time"])
    common_rate = ["-b:v", bv, "-maxrate", bv, "-bufsize", bufsize]
    keyframes = [
        "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0",
        "-force_key_frames", f"expr:gte(t,n_forced*{int(cfg['hls_time'])})",
    ]
    if enc == "libx264":
        return ["-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main",
                "-pix_fmt", "yuv420p", *common_rate, *keyframes]
    if enc == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-profile:v", "main",
                *common_rate, *keyframes]
    if enc == "h264_qsv":
        return ["-c:v", "h264_qsv", "-profile:v", "main", *common_rate, *keyframes]
    if enc == "h264_vaapi":
        # VAAPI needs the frames uploaded; handled via encoder_init_args + vf.
        return ["-c:v", "h264_vaapi", *common_rate, *keyframes]
    # Fallback to libx264 for anything unrecognized.
    return ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
            *common_rate, *keyframes]


def _double_bitrate(bv: str) -> str:
    num = "".join(c for c in bv if c.isdigit() or c == ".")
    suffix = bv[len(num):] or "k"
    try:
        return f"{int(float(num) * 2)}{suffix}"
    except ValueError:
        return "7000k"


def build_input_spec(desc: SourceDescriptor, cfg: Config, work_dir: str) -> dict[str, list[str]]:
    """Return {'pre': [...], 'inputs': [...], 'maps': [...]} for ffmpeg."""
    kind = desc.kind
    if kind == "testpattern":
        return {
            "pre": ["-re"],
            "inputs": [
                "-f", "lavfi", "-i", f"testsrc2=size={cfg['width']}x{cfg['height']}:rate={cfg['fps']}",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            ],
            "maps": ["-map", "0:v:0", "-map", "1:a:0"],
        }

    if kind in ("slideshow", "tutorial"):
        # Rendered to a finite MP4 by the pipeline; stream it on a loop.
        video = desc.params["video_file"]
        return {
            "pre": ["-stream_loop", "-1", "-re"],
            "inputs": ["-i", video],
            "maps": ["-map", "0:v:0", "-map", "0:a:0?"],
        }

    if kind == "file":
        path = desc.params["path"]
        if not os.path.exists(path):
            raise SourceError(f"File not found: {path}")
        pre = ["-re"]
        if desc.params.get("loop"):
            pre = ["-stream_loop", "-1", "-re"]
        return {"pre": pre, "inputs": ["-i", path],
                "maps": ["-map", "0:v:0", "-map", "0:a:0?"]}

    if kind == "youtube_vod":
        # Downloaded to a file by prepare(); treated like a local file.
        path = desc.params["path"]
        return {"pre": ["-re"], "inputs": ["-i", path],
                "maps": ["-map", "0:v:0", "-map", "0:a:0?"]}

    if kind == "youtube_live":
        url = desc.params["stream_url"]
        return {
            "pre": ["-reconnect", "1", "-reconnect_streamed", "1",
                    "-reconnect_delay_max", "5"],
            "inputs": ["-i", url],
            "maps": ["-map", "0:v:0", "-map", "0:a:0?"],
        }

    if kind == "camera":
        cam = cfg["camera"]
        vdev = desc.params.get("video_device", cam["video_device"])
        asrc = desc.params.get("audio_source", cam["audio_source"])
        return {
            "pre": ["-f", "v4l2", "-framerate", str(cam["input_fps"]),
                    "-video_size", cam["input_size"]],
            "inputs": ["-i", vdev, "-f", "pulse", "-i", asrc],
            "maps": ["-map", "0:v:0", "-map", "1:a:0"],
        }

    raise SourceError(f"Unknown source kind: {kind}")


def build_ffmpeg_cmd(desc: SourceDescriptor, cfg: Config, hls_dir: str, work_dir: str) -> list[str]:
    spec = build_input_spec(desc, cfg, work_dir)
    args: list[str] = [cfg["ffmpeg"], "-hide_banner", "-loglevel", "warning", "-nostdin", "-y"]
    args += list(cfg["encoder_init_args"])
    args += spec["pre"]
    args += spec["inputs"]
    args += spec["maps"]
    args += ["-vf", _video_filter(cfg)]
    args += _video_encode_args(cfg)
    args += ["-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2"]
    args += _hls_output_args(desc, cfg, hls_dir)
    return args


def _hls_output_args(desc: SourceDescriptor, cfg: Config, hls_dir: str) -> list[str]:
    list_size = cfg.hls_list_size(desc.kind)
    return [
        "-f", "hls",
        "-hls_time", str(cfg["hls_time"]),
        "-hls_list_size", str(list_size),
        "-hls_flags", "delete_segments+omit_endlist+program_date_time+independent_segments+temp_file",
        "-hls_segment_type", "mpegts",
        "-hls_segment_filename", os.path.join(hls_dir, "seg_%06d.ts"),
        os.path.join(hls_dir, "stream.m3u8"),
    ]


# --- preparation steps (run before ffmpeg) ---------------------------------

def resolve_youtube_live(url: str, cfg: Config) -> str:
    """Resolve a YouTube live URL to a direct stream URL ffmpeg can ingest."""
    if not url:
        raise SourceError("No live URL configured (set presets.weather.url).")
    try:
        out = subprocess.run(
            [cfg["yt_dlp"], "-g", "-f", "best", url],
            capture_output=True, text=True, timeout=60, check=True,
        )
    except FileNotFoundError as exc:
        raise SourceError("yt-dlp is not installed (run scripts/setup.sh).") from exc
    except subprocess.CalledProcessError as exc:
        raise SourceError(f"yt-dlp failed to resolve live URL: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SourceError("yt-dlp timed out resolving the live URL.") from exc
    urls = [line for line in out.stdout.splitlines() if line.strip()]
    if not urls:
        raise SourceError("yt-dlp returned no stream URL.")
    return urls[0]


def download_youtube_vod(url: str, cfg: Config, work_dir: str) -> str:
    """Download the best muxed VOD into work_dir and return the file path."""
    if not url:
        raise SourceError("No YouTube URL provided.")
    out_tmpl = os.path.join(work_dir, "vod.%(ext)s")
    try:
        subprocess.run(
            [cfg["yt_dlp"], "-f", "bv*+ba/b", "--merge-output-format", "mp4",
             "-o", out_tmpl, url],
            check=True, timeout=900,
        )
    except FileNotFoundError as exc:
        raise SourceError("yt-dlp is not installed (run scripts/setup.sh).") from exc
    except subprocess.CalledProcessError as exc:
        raise SourceError("yt-dlp failed to download the video.") from exc
    for name in os.listdir(work_dir):
        if name.startswith("vod."):
            return os.path.join(work_dir, name)
    raise SourceError("Download finished but no file was produced.")


def cmd_to_string(args: list[str]) -> str:
    return " ".join(shlex.quote(a) for a in args)
