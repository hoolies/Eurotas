#!/usr/bin/env python3
"""Eurotas entrypoint: start the broadcast hub (FastAPI + uvicorn).

The media pipeline is started/stopped via the FastAPI lifespan, so uvicorn's
own signal handling gives a clean shutdown (and clears the tmpfs buffer).

Docs once running:
  Swagger UI -> http://HOST:PORT/docs
  ReDoc      -> http://HOST:PORT/redoc
"""

from __future__ import annotations

import argparse

import uvicorn

from eurotas.config import Config
from eurotas.pipeline import PipelineManager
from eurotas.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser(description="Eurotas local broadcast hub")
    parser.add_argument("-c", "--config", default="config.json",
                        help="Path to config JSON (default: config.json)")
    parser.add_argument("--host", help="Override bind host")
    parser.add_argument("--port", type=int, help="Override bind port")
    parser.add_argument("--no-start", action="store_true",
                        help="Start idle (do not auto-start the default channel)")
    parser.add_argument("--log-level", default="info", help="uvicorn log level")
    args = parser.parse_args()

    cfg = Config.load(args.config)
    if args.host:
        cfg.data["host"] = args.host
    if args.port:
        cfg.data["port"] = args.port

    pipeline = PipelineManager(cfg)
    app = create_app(cfg, pipeline, auto_start=not args.no_start)

    host, port = cfg["host"], int(cfg["port"])
    print(f"[eurotas] hub on http://{host}:{port}/  (control: /control, docs: /docs)")
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
