# Eurotas

A local **broadcast hub**: take ONE source at a time (live YouTube, on-demand
YouTube, a local file, a USB camera + mic, or a looping image slideshow),
ingest/encode it **once** into a temporary in-RAM HLS buffer, and fan it out as a
single channel so **every TV plays the same thing within ~1 second of the
others**. Nothing is stored permanently.

This solves the "limited internet, many screens" problem: the internet is used
once per source; the fast LAN carries the rest. (DRM services such as
Netflix/Disney+/Prime cannot be redistributed and are out of scope.)

## How it works

```
source (1 active) -> ffmpeg -> rolling HLS in tmpfs (PROGRAM-DATE-TIME)
                                   |
                          tiny HTTP server (this app)
                                   |
            every screen loads /  (synchronized hls.js player)
            player steers playback to (serverClock - OFFSET) => <1s sync
```

- One ffmpeg process produces a single normalized H.264/AAC HLS rendition.
- The buffer is a rolling window in `tmpfs` (RAM) that auto-deletes old segments.
- Each client syncs its clock to the hub (`/api/time`) and continuously steers
  to `serverNow - OFFSET` using HLS `PROGRAM-DATE-TIME`, so all screens match.
- A watchdog restarts looping sources and **fails over to the tutorial
  slideshow** if a live source stalls or dies.

## Requirements

- Linux box with `ffmpeg` (Intel Quick Sync / VAAPI recommended for HW encode).
- `yt-dlp` (YouTube live + on-demand). `streamlink` optional.
- Python 3.10+ and the Python deps in `requirements.txt` (FastAPI, uvicorn).

## Quick start

```bash
cd eurotas
./scripts/setup.sh                  # installs fastapi/uvicorn/yt-dlp, mounts tmpfs
# (or: python3 -m pip install -r requirements.txt)
cp config.example.json config.json  # set your PIN + Weather Network URL
python3 run.py -c config.json
```

Then:

- TVs / phones / computers open `http://HUB_IP:8080/` -> the synchronized channel.
- You (PC or phone) open `http://HUB_IP:8080/control`, enter the PIN, pick a source.

## API docs (Swagger)

The web layer is **FastAPI**, so the API is fully self-documented:

- Swagger UI: `http://HUB_IP:8080/docs`
- ReDoc: `http://HUB_IP:8080/redoc`
- OpenAPI JSON: `http://HUB_IP:8080/openapi.json`

The channel starts on the **tutorial slideshow** (the always-on idle screen that
explains the app). Use the control page to switch to any source.

## Configuration (`config.json`)

| Key | Meaning |
| --- | --- |
| `control_pin` | PIN required to change the channel. |
| `hls_dir` | tmpfs path for the rolling buffer (RAM). |
| `work_dir` | scratch for VOD downloads / generated slides. |
| `width`/`height`/`fps`/`video_bitrate` | output rendition. |
| `video_encoder` | `libx264` (default), `h264_vaapi`, `h264_qsv`, `h264_nvenc`. |
| `offsets.*` | per-source delay (seconds) = sync anchor; capped by `max_offset` (300). |
| `presets.weather.url` | your exact The Weather Network live URL. |
| `slideshow.*` | image folder + seconds-per-image for the slideshow source. |
| `camera.*` | v4l2 video device + PulseAudio mic source. |

### Hardware encoding

For many screens, enable Quick Sync or VAAPI to offload the single encode:

```json
"video_encoder": "h264_vaapi",
"encoder_init_args": ["-hwaccel", "vaapi", "-vaapi_device", "/dev/dri/renderD128"]
```

## Sources

- **Live TV (YouTube)** - your provided Weather Network URL (saved as a preset).
- **On-demand YouTube** - downloaded once into `work_dir`, then streamed, then deleted.
- **Local file** - any video on the hub (optional loop).
- **USB camera + mic** - `v4l2` + PulseAudio/PipeWire capture.
- **Image slideshow** - a folder of images, N seconds each, looping.
- **Tutorial** - generated slides; the default idle channel.
- **Test pattern** - for verifying sync across screens.

## Clients (TVs)

- **LG (webOS) / Samsung (Tizen) / Android TV**: open `http://HUB_IP:8080/` in the
  built-in browser first. If a TV browser can't run hls.js, use a Fire Stick.
- **Older TVs**: use a Fire Stick (Silk browser / a kiosk browser app) on the URL.
- **Kiosk auto-launch**: set the browser to auto-open the URL at boot so screens
  self-recover after power loss (e.g. "Fully Kiosk Browser" on Android/Fire,
  start URL = the channel page).

> Autoplay with sound: if a TV blocks autoplay, the channel shows a one-tap
> "start" button. Kiosk browsers can be configured to allow autoplay with audio.

## Run as a service (always-on)

- **systemd** (Debian/Ubuntu): edit and install `systemd/eurotas.service`,
  then `sudo systemctl enable --now eurotas`.
- **runit** (Void Linux): `ln -s /opt/eurotas/runit/eurotas /var/service/eurotas`.

On boot it starts on the tutorial slideshow; pick a source any time from control.

## API (for reference)

Full interactive docs at `/docs`. Summary:

| Method | Path | Tag | Notes |
| --- | --- | --- | --- |
| GET | `/` | Pages | channel player |
| GET | `/control` | Pages | control UI |
| GET | `/api/time` | Channel | `{now_ms}` clock sync |
| GET | `/api/channel` | Channel | current channel (source, offset, generation) |
| GET | `/api/sources` | Channel | presets + source kinds |
| POST | `/api/login` | Control | `{pin}` -> session cookie |
| POST | `/api/select` | Control | `{kind, params}` or `{preset}` (auth) |
| POST | `/api/stop` | Control | back to tutorial (auth) |
| GET | `/api/status` | Control | status + ffmpeg log tail (auth) |
| GET | `/hls/{path}` | Media | rolling HLS buffer |

## Verify sync

1. Start the **Test pattern** source.
2. Open `http://HUB_IP:8080/` on several screens.
3. The moving test image / timer should match across screens (<1s). Film them
   side by side to measure.
4. Switch sources from control and confirm all screens follow without manual steps.
5. Stop the hub and confirm `hls_dir` is empty (nothing persisted).
