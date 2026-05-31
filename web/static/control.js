/* Eurotas control UI (PIN-protected). Lets the hub/phone pick the active
 * source. TVs never load this page - they only load the channel view (/).
 */
(function () {
  "use strict";

  var loginSec = document.getElementById("login");
  var consoleSec = document.getElementById("console");
  var pinInput = document.getElementById("pin");
  var loginBtn = document.getElementById("loginBtn");
  var loginErr = document.getElementById("loginErr");
  var presetsEl = document.getElementById("presets");
  var formsEl = document.getElementById("forms");
  var nowSource = document.getElementById("nowSource");
  var nowMeta = document.getElementById("nowMeta");
  var logEl = document.getElementById("log");
  var stopBtn = document.getElementById("stopBtn");

  var sources = null;

  function show(el) { el.classList.remove("hidden"); }
  function hide(el) { el.classList.add("hidden"); }

  async function api(path, opts) {
    var r = await fetch(path, Object.assign({ cache: "no-store" }, opts || {}));
    return r;
  }

  async function checkAuth() {
    var r = await api("/api/status");
    if (r.status === 401) { show(loginSec); hide(consoleSec); return false; }
    show(consoleSec); hide(loginSec);
    return true;
  }

  async function doLogin() {
    loginErr.textContent = "";
    var r = await api("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: pinInput.value }),
    });
    if (r.ok) { await init(); }
    else { loginErr.textContent = "Invalid PIN"; }
  }

  loginBtn.addEventListener("click", doLogin);
  pinInput.addEventListener("keydown", function (e) { if (e.key === "Enter") doLogin(); });

  function fieldInput(kind, field) {
    var id = "f_" + kind + "_" + field;
    if (field === "loop") {
      return '<label class="chk"><input type="checkbox" id="' + id + '"/> loop</label>';
    }
    var ph = {
      url: "https://...", path: "/path/to/video.mp4",
      dir: "/path/to/images", seconds_per_image: "10",
    }[field] || field;
    var type = (field === "seconds_per_image") ? "number" : "text";
    return '<input id="' + id + '" type="' + type + '" placeholder="' + ph + '"/>';
  }

  function renderForms() {
    presetsEl.innerHTML = "";
    var presets = sources.presets || {};
    Object.keys(presets).forEach(function (key) {
      var p = presets[key];
      var b = document.createElement("button");
      b.className = "btn primary";
      b.textContent = "\u25B6 " + (p.title || key);
      b.addEventListener("click", function () { select({ preset: key }); });
      presetsEl.appendChild(b);
    });

    formsEl.innerHTML = "";
    sources.kinds.forEach(function (k) {
      var card = document.createElement("div");
      card.className = "src";
      var fields = (k.fields || []).map(function (f) {
        return fieldInput(k.kind, f);
      }).join(" ");
      card.innerHTML =
        '<div class="src-head">' + k.label + "</div>" +
        '<div class="src-fields">' + fields + "</div>";
      var btn = document.createElement("button");
      btn.className = "btn";
      btn.textContent = "Play";
      btn.addEventListener("click", function () {
        var params = {};
        (k.fields || []).forEach(function (f) {
          var el = document.getElementById("f_" + k.kind + "_" + f);
          if (!el) return;
          if (f === "loop") params[f] = el.checked;
          else if (el.value !== "") params[f] = el.value;
        });
        select({ kind: k.kind, params: params });
      });
      card.appendChild(btn);
      formsEl.appendChild(card);
    });
  }

  async function select(payload) {
    nowMeta.textContent = "switching...";
    var r = await api("/api/select", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    var j = await r.json();
    if (!r.ok || j.ok === false) {
      nowMeta.textContent = "Error: " + (j.error || r.status);
    }
    refresh();
  }

  stopBtn.addEventListener("click", function () {
    api("/api/stop", { method: "POST" }).then(refresh);
  });

  async function refresh() {
    var r = await api("/api/status");
    if (r.status === 401) { return checkAuth(); }
    var j = await r.json();
    nowSource.textContent = (j.title || j.source || "-") +
      (j.status === "running" ? "" : "  [" + j.status + "]");
    var meta = [];
    if (j.source) meta.push(j.source);
    if (j.offset_ms) meta.push("delay " + (j.offset_ms / 1000) + "s");
    if (j.uptime_s) meta.push("up " + j.uptime_s + "s");
    if (j.error) meta.push("error: " + j.error);
    nowMeta.textContent = meta.join("  -  ");
    if (j.log) logEl.textContent = j.log.join("\n");
  }

  async function init() {
    var ok = await checkAuth();
    if (!ok) return;
    var r = await api("/api/sources");
    sources = await r.json();
    renderForms();
    refresh();
    setInterval(refresh, 3000);
  }

  init();
})();
