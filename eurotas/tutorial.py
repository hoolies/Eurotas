"""Generate the default tutorial slideshow and build slideshow concat lists.

The tutorial slideshow doubles as the always-on idle channel: it explains what
Eurotas does and how to use it. Slides are rendered with ffmpeg drawtext so we
need no extra image libraries.
"""

from __future__ import annotations

import os
import subprocess

from .config import Config


_FONT_CANDIDATES = [
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
]

_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif")

BG = "0x0f1a2b"
TITLE_COLOR = "white"
BODY_COLOR = "0xcfe3ff"


def _font_arg() -> str:
    for path in _FONT_CANDIDATES:
        if os.path.exists(path):
            return f"fontfile='{path}'"
    # fontconfig is enabled in this ffmpeg build, so a family name works too.
    return "font='Sans'"


def _drawtext(textfile: str, fontsize: int, color: str, y_expr: str) -> str:
    return (
        f"drawtext={_font_arg()}:textfile='{textfile}':"
        f"fontsize={fontsize}:fontcolor={color}:line_spacing=12:"
        f"x=(w-text_w)/2:y={y_expr}"
    )


def _slides(host_hint: str, port: int) -> list[tuple[str, str]]:
    url = f"http://<hub-ip>:{port}/"
    return [
        ("Eurotas",
         "Watch one source on every screen,\nperfectly in sync."),
        ("How it works",
         "One source is downloaded or captured ONCE,\n"
         "cached briefly in memory, and streamed\n"
         "to all TVs as a single channel."),
        ("Open the channel on a TV",
         f"In the TV browser (or a Fire Stick), open:\n{url}\n"
         "The screen will join the channel automatically."),
        ("Pick what plays",
         "From your phone or the hub PC, open:\n"
         f"http://<hub-ip>:{port}/control\n"
         "Enter the PIN, then choose a source."),
        ("Available sources",
         "Live TV (YouTube)  -  On-demand YouTube\n"
         "Local video file  -  USB camera + mic\n"
         "Image slideshow  -  this tutorial"),
        ("All screens stay in sync",
         "Every TV plays the same moment within\n"
         "one second. TVs cannot pause or stop -\n"
         "it just plays, like a channel."),
        ("Nothing is kept",
         "Content is cached only temporarily in RAM\n"
         "and is erased when the channel changes\n"
         "or the hub restarts."),
    ]


def generate_tutorial_slides(cfg: Config, dest_dir: str) -> list[str]:
    os.makedirs(dest_dir, exist_ok=True)
    w, h = cfg["width"], cfg["height"]
    slides = _slides(cfg["host"], cfg["port"])
    paths: list[str] = []
    for idx, (title, body) in enumerate(slides):
        title_txt = os.path.join(dest_dir, f"title_{idx}.txt")
        body_txt = os.path.join(dest_dir, f"body_{idx}.txt")
        with open(title_txt, "w", encoding="utf-8") as fh:
            fh.write(title)
        with open(body_txt, "w", encoding="utf-8") as fh:
            fh.write(body)
        out = os.path.join(dest_dir, f"slide_{idx:02d}.png")
        title_fs = max(36, int(h * 0.085))
        body_fs = max(24, int(h * 0.05))
        vf = ",".join([
            _drawtext(title_txt, title_fs, TITLE_COLOR, "h*0.18"),
            _drawtext(body_txt, body_fs, BODY_COLOR, "h*0.42"),
        ])
        cmd = [
            cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={BG}:s={w}x{h}",
            "-vf", vf, "-frames:v", "1", out,
        ]
        subprocess.run(cmd, check=True, timeout=60)
        paths.append(out)
    return paths


def _slideshow_vf(cfg: Config) -> str:
    w, h, fps = cfg["width"], cfg["height"], cfg["fps"]
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"fps={fps},format=yuv420p"
    )


def render_slideshow_video(cfg: Config, image_paths: list[str],
                           seconds_per_image: float, dest: str,
                           audio: str = "") -> str:
    """Render still images into a finite MP4 (with audio) for looped streaming.

    Realtime pacing of still images directly into HLS is unreliable in ffmpeg,
    so we render a normal video file here (fast, non-realtime) and the pipeline
    then streams it with `-re -stream_loop -1`, which is robust.
    """
    concat = dest + ".concat"
    build_concat_file(image_paths, seconds_per_image, concat)
    fps = int(cfg["fps"])
    gop = fps * int(cfg["hls_time"])
    if audio and os.path.exists(audio):
        audio_in = ["-stream_loop", "-1", "-i", audio]
    else:
        audio_in = ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
    cmd = [
        cfg["ffmpeg"], "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0", "-i", concat,
        *audio_in,
        "-map", "0:v:0", "-map", "1:a:0",
        "-vf", _slideshow_vf(cfg),
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
        "-r", str(fps), "-g", str(gop), "-keyint_min", str(gop),
        "-force_key_frames", f"expr:gte(t,n_forced*{int(cfg['hls_time'])})",
        "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2",
        "-shortest", dest,
    ]
    subprocess.run(cmd, check=True, timeout=300)
    return dest


def build_concat_file(image_paths: list[str], seconds_per_image: float, dest: str) -> str:
    """Write an ffmpeg concat demuxer list looping over images."""
    if not image_paths:
        raise ValueError("No images to build a slideshow from.")
    lines: list[str] = []
    for path in image_paths:
        lines.append(f"file '{path}'")
        lines.append(f"duration {seconds_per_image}")
    # Concat demuxer needs the last file repeated (without duration) so the
    # final image is shown for its full duration.
    lines.append(f"file '{image_paths[-1]}'")
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return dest


def list_images(directory: str) -> list[str]:
    if not directory or not os.path.isdir(directory):
        return []
    names = sorted(
        n for n in os.listdir(directory)
        if n.lower().endswith(_IMAGE_EXTS)
    )
    return [os.path.join(directory, n) for n in names]
