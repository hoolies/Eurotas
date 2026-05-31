"""Pipeline manager: runs one ffmpeg source at a time into the tmpfs HLS dir.

Responsibilities:
- prepare a source (download VOD / resolve live URL / generate slideshow)
- start/stop the single ffmpeg process
- expose channel status (kind, title, offset, generation) to the server
- watchdog: restart looping sources that die, fail over non-looping sources
  (live/camera/file) to the tutorial slideshow when they stall or exit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from collections import deque

from .config import Config
from .sources import (
    SourceDescriptor,
    SourceError,
    build_ffmpeg_cmd,
    cmd_to_string,
    download_youtube_vod,
    resolve_youtube_live,
)
from . import tutorial


PLAYLIST = "stream.m3u8"


class PipelineManager:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.hls_dir = cfg["hls_dir"]
        self.work_dir = cfg["work_dir"]
        self._lock = threading.RLock()
        self._proc: subprocess.Popen | None = None
        self._reader: threading.Thread | None = None
        self._log = deque(maxlen=60)
        self.current: SourceDescriptor | None = None
        self.generation = 0
        self.status = "idle"
        self.error: str | None = None
        self.started_at = 0.0
        self._last_playlist_mtime = 0.0
        self._last_progress_at = 0.0
        self._restart_attempts = 0
        self._vod_file: str | None = None
        self._wd_stop = threading.Event()
        self._wd_thread: threading.Thread | None = None

        os.makedirs(self.hls_dir, exist_ok=True)
        os.makedirs(self.work_dir, exist_ok=True)

    # --- lifecycle ---------------------------------------------------------
    def start(self, auto: bool = True) -> None:
        self._wd_thread = threading.Thread(target=self._watchdog, daemon=True)
        self._wd_thread.start()
        if auto:
            self.select_default()

    def shutdown(self) -> None:
        self._wd_stop.set()
        with self._lock:
            self.status = "stopped"
            self.current = None
            self._stop_process()
            self._clear_hls()

    # --- public control ----------------------------------------------------
    def select_default(self) -> None:
        self.select("tutorial", {})

    def select(self, kind: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        title = params.pop("title", None) or _default_title(kind, self.cfg)
        desc = SourceDescriptor(kind=kind, title=title, params=params)
        with self._lock:
            self.status = "preparing"
            self.error = None
            self._stop_process()
            self._clear_hls()
            try:
                self._prepare(desc)
                self._start_process(desc)
                self.current = desc
                self.generation += 1
                self.status = "running"
                self._restart_attempts = 0
                self.started_at = time.time()
            except SourceError as exc:
                self.error = str(exc)
                self.status = "error"
                if kind != self.cfg["failover_source"]:
                    self._safe_failover()
                raise
        return self.status_dict()

    # --- preparation -------------------------------------------------------
    def _prepare(self, desc: SourceDescriptor) -> None:
        kind = desc.kind
        if self._vod_file and os.path.exists(self._vod_file):
            try:
                os.remove(self._vod_file)
            except OSError:
                pass
            self._vod_file = None

        if kind == "tutorial":
            slide_dir = os.path.join(self.work_dir, "tutorial")
            slides = tutorial.list_images(slide_dir)
            if not slides:
                slides = tutorial.generate_tutorial_slides(self.cfg, slide_dir)
            video = os.path.join(self.work_dir, "tutorial.mp4")
            if not os.path.exists(video):
                tutorial.render_slideshow_video(self.cfg, slides, 9, video)
            desc.params["video_file"] = video

        elif kind == "slideshow":
            ss = self.cfg["slideshow"]
            directory = desc.params.get("dir") or ss["dir"]
            seconds = float(desc.params.get("seconds_per_image", ss["seconds_per_image"]))
            audio = desc.params.get("audio") or ss.get("audio") or ""
            images = tutorial.list_images(directory)
            if not images:
                raise SourceError(f"No images found in slideshow dir: {directory!r}")
            video = os.path.join(self.work_dir, "slideshow.mp4")
            tutorial.render_slideshow_video(self.cfg, images, seconds, video, audio)
            desc.params["video_file"] = video

        elif kind == "youtube_live":
            url = desc.params.get("url", "")
            desc.params["stream_url"] = resolve_youtube_live(url, self.cfg)

        elif kind == "youtube_vod":
            url = desc.params.get("url", "")
            path = download_youtube_vod(url, self.cfg, self.work_dir)
            desc.params["path"] = path
            self._vod_file = path

        elif kind == "file":
            if not desc.params.get("path"):
                raise SourceError("No file path provided.")

        elif kind in ("camera", "testpattern"):
            pass
        else:
            raise SourceError(f"Unknown source kind: {kind}")

    # --- ffmpeg process ----------------------------------------------------
    def _start_process(self, desc: SourceDescriptor) -> None:
        cmd = build_ffmpeg_cmd(desc, self.cfg, self.hls_dir, self.work_dir)
        self._log.append(f"$ {cmd_to_string(cmd)}")
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except FileNotFoundError as exc:
            raise SourceError(f"ffmpeg not found: {self.cfg['ffmpeg']}") from exc

        self._reader = threading.Thread(target=self._read_stderr, args=(self._proc,), daemon=True)
        self._reader.start()
        self._wait_for_playlist(timeout=12)
        self._last_progress_at = time.time()

    def _wait_for_playlist(self, timeout: float) -> None:
        playlist = os.path.join(self.hls_dir, PLAYLIST)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc and self._proc.poll() is not None:
                tail = " | ".join(list(self._log)[-4:])
                raise SourceError(f"ffmpeg exited during startup: {tail}")
            if os.path.exists(playlist) and _segment_count(self.hls_dir) >= 1:
                self._last_playlist_mtime = os.path.getmtime(playlist)
                return
            time.sleep(0.25)
        # Not fatal: some inputs are slow. Leave it running; watchdog will judge.

    def _read_stderr(self, proc: subprocess.Popen) -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                line = line.rstrip()
                if line:
                    self._log.append(line)
        except (ValueError, OSError):
            pass

    def _stop_process(self) -> None:
        proc = self._proc
        self._proc = None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass

    def _clear_hls(self) -> None:
        if not os.path.isdir(self.hls_dir):
            return
        for name in os.listdir(self.hls_dir):
            if name.endswith((".ts", ".m3u8", ".tmp", ".m4s", ".mp4")):
                try:
                    os.remove(os.path.join(self.hls_dir, name))
                except OSError:
                    pass

    # --- watchdog ----------------------------------------------------------
    def _watchdog(self) -> None:
        while not self._wd_stop.wait(3.0):
            try:
                self._watchdog_tick()
            except Exception as exc:  # never let the watchdog die
                self._log.append(f"watchdog error: {exc}")

    def _watchdog_tick(self) -> None:
        with self._lock:
            if self.status != "running" or self.current is None:
                return
            proc = self._proc
            desc = self.current
            playlist = os.path.join(self.hls_dir, PLAYLIST)
            now = time.time()

            progressed = False
            if os.path.exists(playlist):
                mtime = os.path.getmtime(playlist)
                if mtime > self._last_playlist_mtime:
                    self._last_playlist_mtime = mtime
                    self._last_progress_at = now
                    progressed = True

            died = proc is not None and proc.poll() is not None
            stalled = (not progressed) and (now - self._last_progress_at > self.cfg["stall_timeout"])

            if not died and not stalled:
                return

            reason = "exited" if died else "stalled"
            self._log.append(f"watchdog: source '{desc.kind}' {reason}")

            if desc.looping and self._restart_attempts < 3:
                self._restart_attempts += 1
                self._log.append(f"watchdog: restarting looping source (attempt {self._restart_attempts})")
                self._stop_process()
                self._clear_hls()
                try:
                    self._start_process(desc)
                    self._last_progress_at = time.time()
                    return
                except SourceError as exc:
                    self._log.append(f"watchdog: restart failed: {exc}")

            self._safe_failover()

    def _safe_failover(self) -> None:
        target = self.cfg["failover_source"]
        self._log.append(f"watchdog: failing over to '{target}'")
        try:
            self._stop_process()
            self._clear_hls()
            desc = SourceDescriptor(kind=target, title=_default_title(target, self.cfg), params={})
            self._prepare(desc)
            self._start_process(desc)
            self.current = desc
            self.generation += 1
            self.status = "running"
            self.started_at = time.time()
            self._restart_attempts = 0
        except SourceError as exc:
            self.status = "error"
            self.error = f"failover failed: {exc}"

    # --- introspection -----------------------------------------------------
    def status_dict(self) -> dict:
        with self._lock:
            desc = self.current
            kind = desc.kind if desc else None
            return {
                "status": self.status,
                "error": self.error,
                "source": kind,
                "title": desc.title if desc else None,
                "offset_ms": int(self.cfg.offset_seconds(kind) * 1000) if kind else 0,
                "generation": self.generation,
                "playlist": "/hls/stream.m3u8",
                "looping": desc.looping if desc else False,
                "uptime_s": int(time.time() - self.started_at) if self.started_at else 0,
            }

    def log_tail(self, n: int = 30) -> list[str]:
        return list(self._log)[-n:]


def _segment_count(hls_dir: str) -> int:
    try:
        return sum(1 for n in os.listdir(hls_dir) if n.endswith(".ts"))
    except OSError:
        return 0


def _default_title(kind: str, cfg: Config) -> str:
    titles = {
        "tutorial": "Eurotas Tutorial",
        "slideshow": "Slideshow",
        "testpattern": "Test Pattern",
        "camera": "Live Camera",
        "file": "Local File",
        "youtube_live": "Live TV",
        "youtube_vod": "YouTube Video",
    }
    return titles.get(kind, kind)
