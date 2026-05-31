"""Pydantic models for the Eurotas API (drive the Swagger/OpenAPI schemas)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class TimeResponse(BaseModel):
    """Hub server clock, used by players as the shared sync anchor."""
    now_ms: int = Field(..., description="Hub wall-clock time in epoch milliseconds.",
                        examples=[1780111523245])


class ChannelStatus(BaseModel):
    """The current channel - what every screen should be playing."""
    status: str = Field(..., description="Pipeline state: preparing | running | error | stopped | idle.",
                        examples=["running"])
    error: Optional[str] = Field(None, description="Last error message, if any.")
    source: Optional[str] = Field(None, description="Active source kind.", examples=["tutorial"])
    title: Optional[str] = Field(None, description="Human-readable channel title.", examples=["Live TV"])
    offset_ms: int = Field(0, description="Playback delay behind real time (the per-source sync OFFSET), in ms.",
                          examples=[45000])
    generation: int = Field(0, description="Increments on every source switch; players reload when it changes.",
                           examples=[3])
    playlist: str = Field("/hls/stream.m3u8", description="HLS playlist URL for the channel.")
    looping: bool = Field(False, description="Whether the source loops forever (slideshow/tutorial/test pattern).")
    uptime_s: int = Field(0, description="Seconds the current source has been running.")


class StatusResponse(ChannelStatus):
    """Channel status plus a tail of the ffmpeg log (control/diagnostics)."""
    log: list[str] = Field(default_factory=list, description="Recent ffmpeg/pipeline log lines.")


class SourceKind(BaseModel):
    """A selectable source type and the parameter fields it accepts."""
    kind: str = Field(..., examples=["youtube_live"])
    label: str = Field(..., examples=["Live TV (YouTube)"])
    fields: list[str] = Field(default_factory=list, description="Parameter field names for this source.",
                             examples=[["url"]])


class SourcesResponse(BaseModel):
    """Configured presets and all selectable source kinds."""
    presets: dict[str, Any] = Field(default_factory=dict,
                                    description="Named presets from config (e.g. the Weather Network live).")
    kinds: list[SourceKind] = Field(default_factory=list)


class LoginRequest(BaseModel):
    pin: str = Field(..., description="Control PIN configured on the hub.", examples=["1234"])


class LoginResponse(BaseModel):
    ok: bool = True


class SelectRequest(BaseModel):
    """Switch the active source. Provide either `kind` (+`params`) or a `preset`."""
    kind: Optional[str] = Field(None, description="Source kind to start.",
                               examples=["file"])
    preset: Optional[str] = Field(None, description="Name of a configured preset to start instead of kind.",
                                  examples=["weather"])
    params: dict[str, Any] = Field(default_factory=dict,
                                   description="Source parameters, e.g. {\"url\": \"...\"} or {\"path\": \"...\", \"loop\": true}.")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"preset": "weather"},
                {"kind": "youtube_vod", "params": {"url": "https://youtu.be/..."}},
                {"kind": "file", "params": {"path": "/media/clip.mp4", "loop": True}},
                {"kind": "slideshow", "params": {"dir": "/media/slides", "seconds_per_image": 10}},
                {"kind": "camera", "params": {}},
                {"kind": "testpattern", "params": {}},
            ]
        }
    }


class ChannelResult(BaseModel):
    """Result of a control action."""
    ok: bool
    channel: Optional[ChannelStatus] = None
    error: Optional[str] = None
