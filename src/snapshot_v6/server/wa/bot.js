// ETEC306 -> WhatsApp forwarder. Runs as the unprivileged user "etecwa", no listening ports.
// Subscribes (read-only MQTT account) to home/<device>/event and home/<device>/image/#; forwards
// events whose type is in cfg.types (default: alarm) to cfg.recipients (empty = send nothing).
// Pairing: while not linked, writes the current QR to state/qr.png (+ qr.txt); WhatsApp on the
// secondary phone -> Linked devices -> Link a device -> scan. Or set cfg.pair_phone for a pairing code.
"use strict";
const fs = require("fs");
const path = require("path");
const mqtt = require("mqtt");
const QRCode = require("qrcode");
const pino = require("pino");
const B = require("@whiskeysockets/baileys");
const makeWASocket = B.default || B.makeWASocket;

const DIR = __dirname;
const STATE = path.join(DIR, "state");
const CFG_PATH = path.join(DIR, "config.json");
const log = pino({ level: process.env.LOG_LEVEL || "info" });
fs.mkdirSync(STATE, { recursive: true, mode: 0o700 });

function loadCfg() {
  const c = JSON.parse(fs.readFileSync(CFG_PATH, "utf8"));
  return Object.assign({ device: "etec306", types: ["alarm"], recipients: [], min_interval_s: 60,
    send_image: true, mqtt_url: "mqtts://127.0.0.1:8883", ca_file: path.join(DIR, "ca.crt"),
    image_wait_ms: 4000, pair_phone: "" }, c);
}
let cfg = loadCfg();
fs.watchFile(CFG_PATH, { interval: 5000 }, () => { try { cfg = loadCfg(); log.info("config reloaded"); } catch (e) { log.error("bad config: " + e.message); } });

let mq = null;
const etTime = () => new Date().toLocaleTimeString("en-GB", { timeZone: "America/New_York" });
function pub(sub, obj) {
  if (mq && mq.connected) mq.publish(`home/${cfg.device}/wa/${sub}`, JSON.stringify(obj), { qos: 1, retain: true });
}
let lastStatus = {};
function writeStatus(o) {
  const st = Object.assign({ t: etTime() }, o);
  lastStatus = st;
  fs.writeFileSync(path.join(STATE, "status.json"), JSON.stringify(st, null, 1));
  pub("status", st);
}
let lastQr = {};

// ------------------------------------------------------------------ WhatsApp
let sock = null, linked = false, me = null;
async function startWA() {
  const { state, saveCreds } = await B.useMultiFileAuthState(path.join(STATE, "auth"));
  let version;
  try { version = (await B.fetchLatestBaileysVersion()).version; } catch (e) { version = undefined; }
  sock = makeWASocket({ auth: state, version, logger: log.child({ m: "wa" }, { level: "warn" }),
    printQRInTerminal: false, browser: ["ETEC306", "Server", "1.0"], markOnlineOnConnect: false,
    syncFullHistory: false, shouldSyncHistoryMessage: () => false });
  sock.ev.on("creds.update", saveCreds);
  let codeAsked = false;
  sock.ev.on("connection.update", async (u) => {
    if (u.qr) {
      linked = false;
      await QRCode.toFile(path.join(STATE, "qr.png"), u.qr, { width: 512, margin: 2 });
      lastQr = { t: etTime(), png: (await QRCode.toBuffer(u.qr, { width: 400, margin: 2 })).toString("base64") };
      pub("qr", lastQr);
      fs.writeFileSync(path.join(STATE, "qr.txt"), u.qr);
      writeStatus({ linked: false, waiting: "qr", qr_png: path.join(STATE, "qr.png") });
      log.info("new pairing QR written to state/qr.png (expires in ~20-60 s, a new one follows)");
      if (cfg.pair_phone && !codeAsked && !state.creds.registered) {
        codeAsked = true;
        try {
          const code = await sock.requestPairingCode(cfg.pair_phone.replace(/\D/g, ""));
          fs.writeFileSync(path.join(STATE, "pair_code.txt"), code + "\n");
          log.info("pairing code written to state/pair_code.txt");
        } catch (e) { log.error("pairing code failed: " + e.message); }
      }
    }
    if (u.connection === "open") {
      linked = true; me = sock.user && sock.user.id;
      lastQr = {}; pub("qr", {});
      for (const f of ["qr.png", "qr.txt", "pair_code.txt"]) { try { fs.unlinkSync(path.join(STATE, f)); } catch (e) {} }
      writeStatus({ linked: true, me: (me || "").replace(/:.*@/, "@") });
      log.info("WhatsApp linked");
    }
    if (u.connection === "close") {
      linked = false;
      const code = u.lastDisconnect && u.lastDisconnect.error && u.lastDisconnect.error.output
        ? u.lastDisconnect.error.output.statusCode : 0;
      if (code === B.DisconnectReason.loggedOut) {
        writeStatus({ linked: false, error: "logged out: run etec306-wa-relink" });
        log.error("logged out by the phone; run etec306-wa-relink to pair again");
        return;              // stays idle until relinked (systemd restart after relink)
      }
      writeStatus({ linked: false, reconnecting: true, code });
      setTimeout(() => startWA().catch((e) => log.error(e.message)), 5000);
    }
  });
}

// ------------------------------------------------------------------ forwarding
const images = new Map();        // eid -> {buf, t}
let lastSent = 0;
const toJid = (n) => String(n).replace(/\D/g, "") + "@s.whatsapp.net";

async function forward(ev) {
  if (!cfg.types.includes(ev.type)) return;
  const rec = (cfg.recipients || []).filter((x) => String(x).replace(/\D/g, "").length >= 8);
  if (!rec.length) { log.info(`event ${ev.id} (${ev.type}) not sent: no recipients configured`); return; }
  if (!linked) { log.warn(`event ${ev.id} not sent: WhatsApp not linked`); return; }
  const now = Date.now();
  if (now - lastSent < cfg.min_interval_s * 1000) { log.info(`event ${ev.id} rate-limited`); return; }
  lastSent = now;
  let img = null;
  if (cfg.send_image && ev.image) {
    const eid = ev.image.replace(/\.jpg$/, "");
    for (let i = 0; i < cfg.image_wait_ms / 200 && !images.has(eid); i++) await new Promise((r) => setTimeout(r, 200));
    img = images.has(eid) ? images.get(eid).buf : null;
  }
  const text = `ETEC306 ${ev.type === "alarm" ? "ALARM" : ev.type} ${ev.ts || ""}\n${ev.text || ""}`.slice(0, 1500);
  for (const r of rec) {
    try {
      if (img) await sock.sendMessage(toJid(r), { image: img, caption: text });
      else await sock.sendMessage(toJid(r), { text });
      log.info(`sent ${ev.id} to ***${String(r).slice(-3)}${img ? " with image" : ""}`);
    } catch (e) { log.error(`send failed: ${e.message}`); }
  }
}

function startMQTT() {
  const c = mq = mqtt.connect(cfg.mqtt_url, { username: cfg.mqtt_user, password: cfg.mqtt_password,
    ca: fs.readFileSync(cfg.ca_file), rejectUnauthorized: true, checkServerIdentity: () => undefined,
    clientId: "etec306-wa-" + Math.random().toString(16).slice(2, 8), reconnectPeriod: 5000 });
  const base = `home/${cfg.device}`;
  c.on("connect", () => { log.info("MQTT connected"); c.subscribe([`${base}/event`, `${base}/image/#`], { qos: 1 });
    pub("status", lastStatus); if (lastQr.png || linked) pub("qr", lastQr); });
  c.on("error", (e) => log.error("MQTT " + e.message));
  c.on("message", (topic, payload) => {
    if (topic.startsWith(`${base}/image/`)) {
      images.set(topic.slice(base.length + 7), { buf: payload, t: Date.now() });
      for (const [k, v] of images) if (Date.now() - v.t > 120000) images.delete(k);
      return;
    }
    let ev; try { ev = JSON.parse(payload.toString()); } catch (e) { return; }
    forward(ev).catch((e) => log.error(e.message));
  });
}

// one-off test message: `etec306-wa-test "text"` writes state/test_request.json (root), sent once to recipients
setInterval(async () => {
  const f = path.join(STATE, "test_request.json");
  if (!fs.existsSync(f) || !linked) return;
  let req; try { req = JSON.parse(fs.readFileSync(f, "utf8")); } catch (e) { req = {}; }
  try { fs.unlinkSync(f); } catch (e) { return; }
  const rec = (cfg.recipients || []).filter((x) => String(x).replace(/\D/g, "").length >= 8);
  const text = String(req.text || "ETEC306 test: WhatsApp alerts are working").slice(0, 500);
  for (const r of rec) {
    try {
      const res = await sock.sendMessage(toJid(r), { text });
      lastSent = Date.now();
      log.info(`TEST sent to ***${String(r).slice(-3)} id=${res && res.key ? res.key.id : "?"}`);
    } catch (e) { log.error("TEST send failed: " + e.message); }
  }
}, 2000);

writeStatus({ linked: false, starting: true });
startWA().catch((e) => { log.error(e.message); process.exit(1); });
startMQTT();
