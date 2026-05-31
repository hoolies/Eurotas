"""Configuration loading and derived settings for Eurotas."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field, asdict
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    # Network / server
    "host": "0.0.0.0",
    "port": 8080,
    "control_pin": "1234",

    # Where the rolling HLS buffer lives. Should be a tmpfs (RAM) mount so
    # nothing persists. See scripts/setup.sh for mounting it.
    "hls_dir": "/tmp/eurotas-hls",

    # Working dir for transient downloads (on-demand YouTube) and generated
    # tutorial slides. Cleared on source switch / shutdown.
    "work_dir": "/tmp/eurotas-work",

    # External tools
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "yt_dlp": "yt-dlp",

    # Output video parameters (the single normalized rendition all TVs receive).
    "width": 1280,
    "height": 720,
    "fps": 30,
    "video_bitrate": "3500k",
    # libx264 (universal) | h264_vaapi | h264_qsv | h264_nvenc
    "video_encoder": "libx264",
    # Extra args injected before the video encoder (e.g. VAAPI device init).
    "encoder_init_args": [],
    "vaapi_device": "/dev/dri/renderD128",

    # HLS segmentation
    "hls_time": 2,

    # Per-source playback OFFSET (seconds) = how far behind real-time the
    # channel plays. Same value is applied to every client, which is the sync
    # anchor. Capped at 300s (5 min) per the plan.
    "offsets": {
        "tutorial": 8,
        "slideshow": 8,
        "testpattern": 8,
        "camera": 6,
        "file": 12,
        "youtube_live": 45,
        "youtube_vod": 30,
    },
    "max_offset": 300,

    # Failover: if a non-looping live source stalls/dies, fall back to this.
    "failover_source": "tutorial",
    # Seconds without playlist progress before we declare a stall.
    "stall_timeout": 20,

    # Source presets shown in the control UI.
    "presets": {
        # Fill in your exact The Weather Network live URL here.
        "weather": {
            "kind": "youtube_live",
            "title": "The Weather Network (Live)",
            "url": "",
        }
    },

    # Slideshow defaults (configurable seconds per image).
    "slideshow": {
        "dir": "",            # folder of images for the user slideshow
        "seconds_per_image": 10,
        "audio": "",          # optional background audio file; empty = silent
    },

    # Camera defaults.
    "camera": {
        "video_device": "/dev/video0",
        "audio_source": "default",   # PulseAudio/PipeWire source name
        "input_size": "1280x720",
        "input_fps": 30,
    },
}


@dataclass
class Config:
    data: dict[str, Any] = field(default_factory=lambda: json.loads(json.dumps(DEFAULT_CONFIG)))
    path: str | None = None

    @classmethod
    def load(cls, path: str | None) -> "Config":
        merged = json.loads(json.dumps(DEFAULT_CONFIG))
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                user = json.load(fh)
            _deep_merge(merged, user)
        return cls(data=merged, path=path)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    # --- derived helpers ---
    def offset_seconds(self, source_kind: str) -> int:
        offsets = self.data["offsets"]
        val = offsets.get(source_kind, offsets.get("tutorial", 8))
        return int(min(val, self.data["max_offset"]))

    def hls_list_size(self, source_kind: str) -> int:
        """Enough segments so the target (now-OFFSET) is always still present."""
        offset = self.offset_seconds(source_kind)
        hls_time = max(1, int(self.data["hls_time"]))
        # cover the offset plus a 30s safety margin
        return max(6, math.ceil((offset + 30) / hls_time))

    def as_public_dict(self) -> dict[str, Any]:
        """Config subset safe to expose to the control UI (no secrets)."""
        d = json.loads(json.dumps(self.data))
        d.pop("control_pin", None)
        return d


def _deep_merge(base: dict, override: dict) -> dict:
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base
