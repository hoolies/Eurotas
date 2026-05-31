"""FastAPI application for Eurotas.

Fully documented via OpenAPI/Swagger:
  - Swagger UI   : /docs
  - ReDoc        : /redoc
  - OpenAPI JSON : /openapi.json

Serves the synchronized player, the PIN-protected control UI, the rolling HLS
buffer and the JSON control/clock APIs.
"""

from __future__ import annotations

import os
import posixpath
import secrets
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import Config
from .models import (
    ChannelResult,
    ChannelStatus,
    LoginRequest,
    LoginResponse,
    SelectRequest,
    SourcesResponse,
    StatusResponse,
    TimeResponse,
)
from .pipeline import PipelineManager
from .sources import SourceError


WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
STATIC_DIR = os.path.join(WEB_DIR, "static")

SELECTABLE_KINDS = [
    {"kind": "youtube_live", "label": "Live TV (YouTube)", "fields": ["url"]},
    {"kind": "youtube_vod", "label": "On-demand YouTube", "fields": ["url"]},
    {"kind": "file", "label": "Local file", "fields": ["path", "loop"]},
    {"kind": "camera", "label": "USB camera + mic", "fields": []},
    {"kind": "slideshow", "label": "Image slideshow", "fields": ["dir", "seconds_per_image"]},
    {"kind": "testpattern", "label": "Test pattern", "fields": []},
    {"kind": "tutorial", "label": "Tutorial (default)", "fields": []},
]

TAGS_METADATA = [
    {"name": "Pages", "description": "HTML pages: the channel player and the control UI."},
    {"name": "Channel", "description": "Public, unauthenticated endpoints the players poll to stay in sync."},
    {"name": "Control", "description": "PIN-protected endpoints to change what is broadcasting."},
    {"name": "Media", "description": "The rolling HLS buffer (served from tmpfs)."},
]

DESCRIPTION = """
Eurotas is a **local broadcast hub**. One source at a time (live YouTube,
on-demand YouTube, a local file, a USB camera + mic, or a looping image
slideshow) is ingested once into a temporary in-RAM HLS buffer and fanned out as
a single channel, so **every screen plays the same moment within ~1 second**.

**Sync model:** players read the hub clock from `GET /api/time`, then steer to
`serverNow - offset_ms` using the HLS `PROGRAM-DATE-TIME` timestamps.

**Control:** authenticate once with the PIN via `POST /api/login` (sets a session
cookie), then use the `Control` endpoints. TVs only ever load `/` and never need
to authenticate.
""".strip()


def _safe_join(base: str, rel: str) -> str | None:
    rel = posixpath.normpath("/" + rel).lstrip("/")
    target = os.path.normpath(os.path.join(base, rel))
    if os.path.commonpath([os.path.abspath(base), os.path.abspath(target)]) != os.path.abspath(base):
        return None
    return target


def create_app(cfg: Config, pipeline: PipelineManager, auto_start: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        pipeline.start(auto=auto_start)
        try:
            yield
        finally:
            pipeline.shutdown()

    app = FastAPI(
        title="Eurotas Hub API",
        version=__version__,
        description=DESCRIPTION,
        openapi_tags=TAGS_METADATA,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.cfg = cfg
    app.state.pipeline = pipeline
    app.state.sessions = set()

    def require_auth(request: Request) -> None:
        token = request.cookies.get("eurotas_session", "")
        if not token or token not in request.app.state.sessions:
            raise HTTPException(status_code=401, detail="Unauthorized: log in with the PIN first.")

    # --- Pages ------------------------------------------------------------
    @app.get("/", tags=["Pages"], include_in_schema=False)
    def player_page() -> FileResponse:
        return FileResponse(os.path.join(WEB_DIR, "player.html"))

    @app.get("/control", tags=["Pages"], include_in_schema=False)
    def control_page() -> FileResponse:
        return FileResponse(os.path.join(WEB_DIR, "control.html"))

    # --- Channel (public) -------------------------------------------------
    @app.get("/api/time", tags=["Channel"], response_model=TimeResponse,
             summary="Hub clock", description="Returns the hub wall-clock time. Players sample this to align to a shared clock.")
    def api_time() -> TimeResponse:
        return TimeResponse(now_ms=int(time.time() * 1000))

    @app.get("/api/channel", tags=["Channel"], response_model=ChannelStatus,
             summary="Current channel",
             description="The active source, its sync OFFSET and the generation counter. Players poll this to detect source switches.")
    def api_channel() -> ChannelStatus:
        return ChannelStatus(**pipeline.status_dict())

    @app.get("/api/sources", tags=["Channel"], response_model=SourcesResponse,
             summary="Available sources",
             description="Configured presets plus the selectable source kinds and their parameter fields.")
    def api_sources() -> SourcesResponse:
        return SourcesResponse(presets=cfg["presets"], kinds=SELECTABLE_KINDS)

    # --- Control (auth) ---------------------------------------------------
    @app.post("/api/login", tags=["Control"], response_model=LoginResponse,
              summary="Log in with PIN",
              responses={403: {"description": "Invalid PIN"}},
              description="Validates the control PIN and sets the `eurotas_session` cookie used by the other control endpoints.")
    def api_login(body: LoginRequest, response: Response) -> LoginResponse:
        if body.pin and secrets.compare_digest(body.pin, str(cfg["control_pin"])):
            token = secrets.token_urlsafe(24)
            app.state.sessions.add(token)
            response.set_cookie("eurotas_session", token, max_age=86400,
                                httponly=True, samesite="lax", path="/")
            return LoginResponse(ok=True)
        raise HTTPException(status_code=403, detail="Invalid PIN")

    @app.post("/api/select", tags=["Control"], response_model=ChannelResult,
              dependencies=[Depends(require_auth)],
              summary="Switch source",
              responses={401: {"description": "Not authenticated"},
                         400: {"description": "Bad request / source error", "model": ChannelResult}},
              description="Start a source by `kind` (+`params`) or by `preset`. Returns the new channel status.")
    def api_select(body: SelectRequest) -> ChannelResult:
        kind = body.kind or ""
        params = dict(body.params or {})
        if body.preset:
            presets = cfg["presets"]
            if body.preset not in presets:
                return JSONResponse(status_code=400,
                                    content={"ok": False, "error": f"unknown preset {body.preset}"})
            p = dict(presets[body.preset])
            kind = p.pop("kind")
            params = {**p, **params}
        if not kind:
            return JSONResponse(status_code=400, content={"ok": False, "error": "missing kind"})
        try:
            status = pipeline.select(kind, params)
            return ChannelResult(ok=True, channel=ChannelStatus(**status))
        except SourceError as exc:
            return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})

    @app.post("/api/stop", tags=["Control"], response_model=ChannelResult,
              dependencies=[Depends(require_auth)],
              summary="Return to tutorial",
              responses={401: {"description": "Not authenticated"}},
              description="Stop the current source and return to the default tutorial slideshow channel.")
    def api_stop() -> ChannelResult:
        pipeline.select_default()
        return ChannelResult(ok=True, channel=ChannelStatus(**pipeline.status_dict()))

    @app.get("/api/status", tags=["Control"], response_model=StatusResponse,
             dependencies=[Depends(require_auth)],
             summary="Status + log",
             responses={401: {"description": "Not authenticated"}},
             description="Channel status plus a tail of the ffmpeg/pipeline log for diagnostics.")
    def api_status() -> StatusResponse:
        data = pipeline.status_dict()
        data["log"] = pipeline.log_tail(30)
        return StatusResponse(**data)

    # --- Media (HLS) ------------------------------------------------------
    @app.get("/hls/{file_path:path}", tags=["Media"],
             summary="HLS buffer",
             description="Serves the rolling HLS playlist and segments from the tmpfs buffer.")
    def hls(file_path: str) -> FileResponse:
        safe = _safe_join(cfg["hls_dir"], file_path)
        if safe is None or not os.path.isfile(safe):
            raise HTTPException(status_code=404, detail="Not found")
        ext = os.path.splitext(safe)[1].lower()
        media_type = {
            ".m3u8": "application/vnd.apple.mpegurl",
            ".ts": "video/mp2t",
        }.get(ext, "application/octet-stream")
        return FileResponse(safe, media_type=media_type,
                            headers={"Cache-Control": "no-cache, no-store"})

    # static assets (vendored hls.js, player/control JS, CSS)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app
