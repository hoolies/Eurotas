/* Eurotas synchronized player.
 *
 * Every screen runs this identical logic: it syncs its clock to the hub,
 * then continuously steers playback toward (serverNow - OFFSET) using the
 * HLS PROGRAM-DATE-TIME timestamps, so all TVs render the same moment within
 * well under a second.
 */
(function () {
  "use strict";

  var video = document.getElementById("video");
  var overlay = document.getElementById("overlay");
  var enableBtn = document.getElementById("enableBtn");
  var statusEl = document.getElementById("status");

  var clockOffset = 0;          // add to Date.now() to get hub server time (ms)
  var channel = null;           // last /api/channel response
  var hls = null;
  var latestDetails = null;     // hls.js level details (has fragments + PDT)
  var usingNative = false;

  // sync tuning
  var HARD_SEEK = 2.0;          // seconds: jump if off by more than this
  var SOFT_BAND = 0.5;          // seconds: nudge playbackRate if off by more than this
  var MAX_RATE_ADJ = 0.08;      // max +/- playback rate trim

  function setStatus(txt) {
    if (statusEl) statusEl.textContent = txt;
  }

  function serverNow() {
    return Date.now() + clockOffset;
  }

  async function syncClock(samples) {
    samples = samples || 5;
    var bestRtt = Infinity;
    var bestOffset = clockOffset;
    for (var i = 0; i < samples; i++) {
      var t0 = performance.now();
      try {
        var r = await fetch("/api/time", { cache: "no-store" });
        var j = await r.json();
        var t1 = performance.now();
        var rtt = t1 - t0;
        var localMid = Date.now() - rtt / 2;
        var offset = j.now_ms - localMid;
        if (rtt < bestRtt) { bestRtt = rtt; bestOffset = offset; }
      } catch (e) { /* retry */ }
    }
    clockOffset = bestOffset;
  }

  async function fetchChannel() {
    var r = await fetch("/api/channel", { cache: "no-store" });
    return await r.json();
  }

  function teardown() {
    if (hls) { try { hls.destroy(); } catch (e) {} hls = null; }
    latestDetails = null;
  }

  function loadChannel(ch) {
    teardown();
    channel = ch;
    var src = ch.playlist + "?g=" + ch.generation;

    if (window.Hls && Hls.isSupported()) {
      usingNative = false;
      hls = new Hls({
        liveSyncDuration: 6,
        liveMaxLatencyDuration: 600,   // we steer manually; allow a deep buffer
        maxBufferLength: 30,
        backBufferLength: 600,
        enableWorker: true,
        lowLatencyMode: false,
      });
      hls.on(Hls.Events.LEVEL_UPDATED, function (_e, data) {
        latestDetails = data.details;
      });
      hls.on(Hls.Events.ERROR, function (_e, data) {
        if (data.fatal) {
          setStatus("recovering...");
          switch (data.type) {
            case Hls.ErrorTypes.NETWORK_ERROR: hls.startLoad(); break;
            case Hls.ErrorTypes.MEDIA_ERROR: hls.recoverMediaError(); break;
            default: setTimeout(reload, 1500); break;
          }
        }
      });
      hls.loadSource(src);
      hls.attachMedia(video);
    } else {
      // Native HLS (Safari / iOS / some TVs)
      usingNative = true;
      video.src = src;
    }
    tryPlay();
  }

  function tryPlay() {
    var p = video.play();
    if (p && p.catch) {
      p.catch(function () {
        // Autoplay with sound blocked -> show a one-tap enable button.
        overlay.classList.remove("hidden");
      });
    }
  }

  enableBtn.addEventListener("click", function () {
    video.muted = false;
    overlay.classList.add("hidden");
    video.play();
  });

  function targetMediaTime() {
    var targetWall = serverNow() - (channel.offset_ms || 0);

    if (usingNative) {
      var sd = video.getStartDate ? video.getStartDate() : null;
      if (sd && !isNaN(sd.getTime())) {
        return (targetWall - sd.getTime()) / 1000;
      }
      return null;
    }

    if (!latestDetails || !latestDetails.fragments || !latestDetails.fragments.length) {
      return null;
    }
    var frags = latestDetails.fragments;
    for (var i = 0; i < frags.length; i++) {
      var f = frags[i];
      var pdt = f.programDateTime;
      if (pdt == null) continue;
      if (targetWall >= pdt && targetWall < pdt + f.duration * 1000) {
        return f.start + (targetWall - pdt) / 1000;
      }
    }
    // Outside the available window: clamp to nearest edge.
    var first = frags[0], last = frags[frags.length - 1];
    if (first.programDateTime != null && targetWall < first.programDateTime) {
      return first.start;
    }
    if (last.programDateTime != null) {
      return last.start + last.duration;
    }
    return null;
  }

  function syncTick() {
    if (!channel || video.readyState < 2 || video.seeking) return;
    var desired = targetMediaTime();
    if (desired == null || desired < 0) return;

    var diff = desired - video.currentTime;
    var ad = Math.abs(diff);
    if (ad > HARD_SEEK) {
      try { video.currentTime = desired; } catch (e) {}
      video.playbackRate = 1;
      setStatus(channel.title || "");
    } else if (ad > SOFT_BAND) {
      var trim = Math.max(-MAX_RATE_ADJ, Math.min(MAX_RATE_ADJ, diff * 0.2));
      video.playbackRate = 1 + trim;
      setStatus((channel.title || "") + "  (sync " + diff.toFixed(2) + "s)");
    } else {
      video.playbackRate = 1;
      setStatus(channel.title || "");
    }
  }

  async function reload() {
    try {
      var ch = await fetchChannel();
      loadChannel(ch);
    } catch (e) {
      setStatus("waiting for hub...");
      setTimeout(reload, 2000);
    }
  }

  async function pollChannel() {
    try {
      var ch = await fetchChannel();
      if (!channel || ch.generation !== channel.generation) {
        loadChannel(ch);
      } else {
        channel.offset_ms = ch.offset_ms;
        channel.title = ch.title;
      }
    } catch (e) { /* keep playing */ }
  }

  // keep audio on; if the device forced mute, surface the enable button
  video.addEventListener("volumechange", function () {
    if (video.muted) overlay.classList.remove("hidden");
  });

  async function boot() {
    setStatus("syncing clock...");
    await syncClock(6);
    await reload();
    setInterval(syncTick, 1000);
    setInterval(pollChannel, 3000);
    setInterval(function () { syncClock(3); }, 60000); // re-sync clock periodically
  }

  boot();
})();
