"""Eurotas - local broadcast hub.

One active source at a time is ingested/encoded once into a temporary rolling
HLS buffer (tmpfs) and fanned out to every screen via a synchronized hls.js
player so all TVs stay within <1s of each other.
"""

__version__ = "0.1.0"
