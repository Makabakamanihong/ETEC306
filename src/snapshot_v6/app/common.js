// shared helpers for index.html / settings.html / enroll.html (plain JS, no libraries)
function $(id) { return document.getElementById(id); }
function f1(v) { return (v === null || v === undefined) ? "-" : Number(v).toFixed(1); }
function post(url, obj) {
  return fetch(url, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(obj || {})})
    .then(function (r) { return r.json(); });
}
function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return "&#" + c.charCodeAt(0) + ";"; }); }
// draw detections over the video. opt: {calib: true} also draws motion blobs / calibration candidate
function drawOverlay(c, s, opt) {
  var g = c.getContext("2d"), W = c.width, H = c.height;
  g.clearRect(0, 0, W, H);
  g.lineWidth = 1; g.strokeStyle = "white";
  g.beginPath(); g.moveTo(W / 2 - 8, H / 2); g.lineTo(W / 2 + 8, H / 2); g.moveTo(W / 2, H / 2 - 8); g.lineTo(W / 2, H / 2 + 8); g.stroke();
  g.font = "12px sans-serif";
  function box(b, color, text) {
    g.strokeStyle = color; g.lineWidth = 2; g.strokeRect(b.x * W, b.y * H, b.w * W, b.h * H);
    if (text) { g.fillStyle = color; g.fillRect(b.x * W, b.y * H - 14, g.measureText(text).width + 4, 14); g.fillStyle = "black"; g.fillText(text, b.x * W + 2, b.y * H - 3); }
  }
  (s.objects.items || []).forEach(function (o) {
    if (o.label === "person") box(o, o.score >= (s.settings.person_conf || 0.5) ? "#39f" : "#9bd", (o.score >= (s.settings.person_conf || 0.5) ? "人 " : "人? ") + o.score);
    else if (o.label === "hand") box(o, "orange", "手 " + o.score);
    else box(o, "#ccc", o.cn + " " + o.score);
  });
  var v = s.vision;
  if (v && v.fresh) {
    (v.faces || []).forEach(function (f) { box(f, Math.abs(f.yaw || 0) > (s.settings.identify_max_yaw_deg || 25) ? "#9f9" : "lime", "脸 " + f.score + (f.yaw !== undefined ? " 转" + Math.round(f.yaw) + "°" : "")); });
    var tk = v.target ? v.target.kind : "";
    if (tk === "head" || tk === "profile" || tk === "face_weak") { var hb = v.target.box; box({x: hb[0], y: hb[1], w: hb[2], h: hb[3]}, "#fc0", {profile: "侧脸", head: "跟踪头部", face_weak: "弱人脸"}[tk]); }
    if (v.target) { g.strokeStyle = "yellow"; g.lineWidth = 2; g.beginPath(); g.moveTo(W / 2, H / 2); g.lineTo(v.target.u * W / 1280, v.target.v * H / 720); g.stroke(); }
    if ((opt && opt.calib) || (s.guide && s.guide.mode === "search")) (v.motion || []).forEach(function (m) { box(m, "#f6f", "动"); });
  }
  // radar tracks projected into the image; the track the vision target is associated with gets a labelled box
  var _labs = [];
  if (v && v.fresh && v.target && v.target.box) _labs.push([v.target.box[0] * W, v.target.box[1] * H - 16, Math.max(v.target.box[2] * W, 90), 16]);
  var vtid = s.vt && s.vt.active ? s.vt.track_id : null;
  (s.tracks || []).forEach(function (t) {
    if (!t.proj) return;
    // every track from its first frame: confirmed = magenta, unconfirmed = dim, object = grey
    var mc = t.kind === "object" ? "#999" : (t.confirmed ? "magenta" : "rgba(255,0,255,0.45)");
    var lab = "T" + t.id + (t.confirmed ? "" : "?") + (t.kind === "object" ? " obj" : "") + " " + Number(t.r_m).toFixed(2) + "m " + (t.speed_cms > 0 ? "+" : "") + t.speed_cms + "cm/s";
    if (t.id === vtid && v && v.fresh && v.target && v.target.box) {
      var tb = v.target.box; box({x: tb[0], y: tb[1] - 0.02, w: tb[2], h: tb[3] + 0.02}, "cyan", lab);
      return;
    }
    var u = Math.min(Math.max(t.proj[0], 0.02), 0.98) * W, q = Math.min(Math.max(t.proj[1], 0.03), 0.97) * H;
    var inside = t.proj[0] >= 0 && t.proj[0] <= 1 && t.proj[1] >= 0 && t.proj[1] <= 1;
    g.strokeStyle = mc; g.lineWidth = t.confirmed ? 2 : 1; g.setLineDash(inside ? [4, 3] : [2, 2]);
    g.beginPath(); g.arc(u, q, inside ? 14 : 8, 0, 7); g.moveTo(u - 20, q); g.lineTo(u + 20, q); g.moveTo(u, q - 20); g.lineTo(u, q + 20); g.stroke();
    g.setLineDash([]); g.fillStyle = "magenta";
    var txt = (inside ? "" : (t.proj[0] < 0 ? "< " : "> ")) + lab + " (radar)", tw = g.measureText(txt).width;
    var tx = Math.max(2, Math.min(u + 16, W - tw - 2)), ty = Math.max(12, Math.min(q - 16, H - 4));
    if (tx + tw > W - 2) tx = W - tw - 2;
    _labs.forEach(function (L) { if (tx < L[0] + L[2] && tx + tw > L[0] && ty > L[1] - 2 && ty - 12 < L[1] + L[3]) ty = Math.min(H - 4, L[1] + L[3] + 26); });
    g.fillStyle = "rgba(255,255,255,0.7)"; g.fillRect(tx - 1, ty - 11, tw + 2, 14); g.fillStyle = t.kind === "object" ? "#666" : (t.confirmed ? "#c0c" : "#d7d");
    g.fillText(txt, tx, ty); _labs.push([tx, ty - 12, tw, 14]);
  });
  // identity label on the tracked face: green name + score when known, red when unknown
  var idn = s.identity || {}, labels = [];
  if (v && v.fresh && v.target && v.target.box && ["face", "face_weak", "profile", "head"].indexOf(v.target.kind) >= 0 && idn.status) {
    var ib = v.target.box, known = idn.status === "known";
    var it = known ? (idn.name || "") + " " + (idn.score || "") : (idn.status === "unknown" ? "UNKNOWN " + (idn.score || "") : "ID?");
    box({x: ib[0], y: ib[1], w: ib[2], h: ib[3]}, known ? "#0c0" : (idn.status === "unknown" ? "red" : "yellow"), it);
    labels.push([ib[0] * W, ib[1] * H - 14, ib[2] * W, 14]);
  }
  if (s.guide && s.guide.mode === "search") { g.fillStyle = "magenta"; g.fillText("SEARCH " + (s.search.i % s.search.n + 1) + "/" + s.search.n, 6, H - 8); }
  if (opt && opt.calib && s.calib.cand) { var b = s.calib.cand.box; box({x: b[0], y: b[1], w: b[2], h: b[3]}, "red", "候选 " + s.calib.cand.kind); }
}
// enlarged face in a small window (top-right of the video)
function drawFace(img, c, s) {
  var v = s.vision, t = v && v.fresh ? v.target : null;
  if (!t || ["face", "face_weak", "profile", "head"].indexOf(t.kind) < 0 || !img.naturalWidth) { c.style.display = "none"; return; }
  var b = t.box, W = img.naturalWidth, H = img.naturalHeight, m = 0.35;
  var cx = (b[0] + b[2] / 2) * W, cy = (b[1] + b[3] / 2) * H, sz = Math.max(b[2] * W, b[3] * H) * (1 + 2 * m);
  try { c.getContext("2d").drawImage(img, cx - sz / 2, cy - sz / 2, sz, sz, 0, 0, c.width, c.height); c.style.display = "block"; }
  catch (e) { c.style.display = "none"; }
}
function drawPlot(c, s, R) {
  var g = c.getContext("2d"), W = c.width, H = c.height;
  var sc = (H - 14) / R, ox = W / 2, oy = H - 6;
  function X(x) { return ox + x * sc; } function Y(y) { return oy - y * sc; }
  g.clearRect(0, 0, W, H); g.strokeStyle = "#bbb"; g.fillStyle = "#666"; g.font = "10px sans-serif"; g.lineWidth = 1;
  var step = R > 3000 ? 1000 : 500, r;
  for (r = step; r <= R; r += step) { g.beginPath(); g.arc(ox, oy, r * sc, Math.PI * (1.5 - 1 / 3), Math.PI * (1.5 + 1 / 3)); g.stroke(); g.fillText((r / 1000) + "m", ox + 2, oy - r * sc + 10); }
  g.fillText("左", 2, 10); g.fillText("右", W - 12, 10);
  var pc = s.guide.pan_cmd * Math.PI / 180; g.strokeStyle = "green"; g.lineWidth = 2;
  g.beginPath(); g.moveTo(ox, oy); g.lineTo(ox + Math.sin(pc) * R * sc, oy - Math.cos(pc) * R * sc); g.stroke(); g.lineWidth = 1;
  (s.clutter || []).forEach(function (k) { g.strokeStyle = "#999"; g.strokeRect(X(k.x_mm) - 5, Y(k.y_mm) - 5, 10, 10); });
  (s.radar.targets || []).forEach(function (t) { g.fillStyle = t.valid ? "#888" : "#ddd"; g.beginPath(); g.arc(X(t.xr_mm), Y(t.y_mm), 2.5, 0, 7); g.fill(); });
  var pid = s.primary ? s.primary.id : null;
  s.tracks.forEach(function (t) {
    var isP = t.id === pid, col = t.kind === "object" ? "#aaa" : (t.kind === "person" ? "red" : "blue");
    g.strokeStyle = col; g.fillStyle = col;
    g.beginPath(); g.arc(X(t.xr_mm), Y(t.y_mm), isP ? 7 : 5, 0, 7); if (isP) g.fill(); else g.stroke();
    g.fillText("T" + t.id + (t.kind === "object" ? "(物)" : ""), X(t.xr_mm) + 8, Y(t.y_mm) - 4);
  });
  g.fillStyle = "black"; g.fillRect(ox - 5, oy - 3, 10, 6);
}
// ---------------------------------------------------------------- language (English default, 中文 toggle)
var LANG = (function () { try { return localStorage.getItem("etec_lang") || "en"; } catch (e) { return "en"; } })();
var _I18N_RE = /[\u3000-\u303f\u4e00-\u9fff\uff00-\uffef\u2014\u2026\u201c\u201d\u00b7\u2192\u2190\u00d7]*[\u4e00-\u9fff][\u3000-\u303f\u4e00-\u9fff\uff00-\uffef\u2014\u2026\u201c\u201d\u00b7]*/g;
var _PUNCT = {"，": ", ", "。": ". ", "：": ": ", "（": " (", "）": ")", "“": "\"", "”": "\"", "；": "; ", "、": ", ", "…": "...", "　": " ", "！": "!", "？": "?", "—": "-", "·": "."};
function _punct(s) { return s.replace(/[，。：（）“”；、…　！？—·]/g, function (c) { return _PUNCT[c]; }); }
var _I18N_N = null;
function _norm() {
  // punctuation-free key -> trimmed value, used for the greedy longest-match fallback
  if (_I18N_N) return _I18N_N;
  _I18N_N = {}; var d = window.I18N_EN || {}, mx = 1;
  Object.keys(d).forEach(function (k) { var n = k.replace(/[^\u4e00-\u9fff]/g, ""); if (n && (_I18N_N[n] === undefined || n === k)) { _I18N_N[n] = d[k].replace(/^[\s(,.:;]+|[\s),.:;]+$/g, ""); mx = Math.max(mx, n.length); } });
  _I18N_N._mx = mx; return _I18N_N;
}
function _run(r) {
  var d = window.I18N_EN || {};
  if (d[r] !== undefined) return d[r];
  var N = _norm();
  var out = r.replace(/[\u4e00-\u9fff]+/g, function (p) {
    var res = [], i = 0;
    while (i < p.length) {
      var j = Math.min(p.length, i + N._mx);
      while (j > i + 1 && N[p.slice(i, j)] === undefined) j--;
      res.push(N[p.slice(i, j)] !== undefined ? N[p.slice(i, j)] : p.slice(i, j)); i = j;
    }
    return " " + res.join(" ") + " ";
  });
  return _punct(out).replace(/\s+/g, " ");
}
function T(s) {
  if (LANG !== "en" || s === null || s === undefined) return s;
  s = String(s);
  if (!/[\u4e00-\u9fff]/.test(s)) return s;
  return _punct(s.replace(_I18N_RE, _run)).replace(/ {2,}/g, " ").replace(/ ([,.:)])/g, "$1").replace(/\( /g, "(");
}
var _DONE = new WeakMap();   // text node -> our own last output (never re-translate it: no observer loop)
function _trText(n) {
  var v = n.nodeValue;
  if (!/[\u4e00-\u9fff]/.test(v) || _DONE.get(n) === v) return;
  var o = T(v);
  _DONE.set(n, o);
  if (o !== v) n.nodeValue = o;
}
function _trNode(n) {
  if (n.nodeType === 3) { _trText(n); return; }
  if (n.nodeType !== 1 || n.tagName === "SCRIPT" || n.tagName === "STYLE" || n.classList.contains("noi18n")) return;
  ["placeholder", "title", "value", "aria-label"].forEach(function (a) {
    var v = n.getAttribute(a);
    if (v && /[\u4e00-\u9fff]/.test(v) && T(v) !== v && !(a === "value" && n.tagName === "INPUT" && n.type !== "button" && n.type !== "submit")) n.setAttribute(a, T(v));
  });
  if (n.tagName === "OPTION" && /[\u4e00-\u9fff]/.test(n.text) && T(n.text) !== n.text) n.text = T(n.text);
  for (var c = n.firstChild; c; c = c.nextSibling) _trNode(c);
}
if (LANG === "en") {
  // canvas labels, dialogs
  var _ft = CanvasRenderingContext2D.prototype.fillText, _mt = CanvasRenderingContext2D.prototype.measureText;
  CanvasRenderingContext2D.prototype.fillText = function (t, x, y, w) { return w === undefined ? _ft.call(this, T(t), x, y) : _ft.call(this, T(t), x, y, w); };
  CanvasRenderingContext2D.prototype.measureText = function (t) { return _mt.call(this, T(t)); };
  var _al = window.alert, _cf = window.confirm, _pr = window.prompt;
  window.alert = function (m) { return _al.call(window, T(m)); };
  window.confirm = function (m) { return _cf.call(window, T(m)); };
  window.prompt = function (m, d) { return _pr.call(window, T(m), d); };
}
document.addEventListener("DOMContentLoaded", function () {
  document.documentElement.lang = LANG === "en" ? "en" : "zh-CN";
  var b = document.createElement("button");
  b.id = "langbtn"; b.className = "noi18n"; b.textContent = LANG === "en" ? "中文" : "English";
  b.style.cssText = "position:fixed;top:4px;right:4px;z-index:99;font-size:12px;padding:2px 8px";
  b.onclick = function () { try { localStorage.setItem("etec_lang", LANG === "en" ? "zh" : "en"); } catch (e) {} location.reload(); };
  document.body.appendChild(b);
  if (LANG !== "en") return;
  document.title = T(document.title);
  _trNode(document.body);
  new MutationObserver(function (ms) {
    ms.forEach(function (m) {
      if (m.type === "characterData") _trText(m.target);
      else m.addedNodes.forEach(_trNode);
    });
  }).observe(document.body, {childList: true, subtree: true, characterData: true});
});

// pause the MJPEG stream in hidden tabs (each stream holds one of the browser's ~6 connections per host)
document.addEventListener("visibilitychange", function () {
  var imgs = document.querySelectorAll("img[src*='stream.mjpg'], img[data-stream]");
  Array.prototype.forEach.call(imgs, function (im) {
    if (document.hidden) { if (!im.dataset.stream) { im.dataset.stream = "1"; im._oe = im.onerror; im.onerror = null; im.src = "data:,"; } }
    else if (im.dataset.stream) { delete im.dataset.stream; im.src = "/api/stream.mjpg?" + Date.now(); var oe = im._oe; setTimeout(function () { im.onerror = oe; }, 500); }
  });
});
