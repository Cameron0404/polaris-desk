/* Polaris page. One poll of /api/desk every 3 s draws Home and the badges; each screen fetches
   its own module when shown. Every API call carries the token in the X-Polaris-Token header. The
   token arrives in the URL fragment from the launcher, never in the page itself. */
"use strict";
// The launcher opens the page at #t=<token>/<screen>. The fragment never reaches a server; the
// token moves to sessionStorage and leaves the address bar at once.
const TOKEN = (() => {
  const m = location.hash.match(/^#t=([A-Za-z0-9_-]+)(?:\/(\w+))?/);
  if (m) {
    try { sessionStorage.setItem("polaris-token", m[1]); } catch (e) { /* storage blocked: this load still works */ }
    history.replaceState(null, "", "#" + (m[2] || "home"));
    return m[1];
  }
  try { return sessionStorage.getItem("polaris-token") || ""; } catch (e) { return ""; }
})();
const HEADLESS = /HeadlessChrome/.test(navigator.userAgent);
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const S = {screen: "home", desk: null, q: null, qDetails: false, chip: "auto", paused: false, orb: null, pinHidden: false};
const store = {get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private window */ } }};

async function getJ(p, signal) { const r = await fetch(p, {cache: "no-store", headers: {"X-Polaris-Token": TOKEN}, signal}); return r.json(); }
async function post(p, body) {
  const r = await fetch(p, {method: "POST", headers: {"Content-Type": "application/json", "X-Polaris-Token": TOKEN}, body: JSON.stringify(body)});
  let j = {}; try { j = await r.json(); } catch (e) { j = {ok: false, error: `the desk answered ${r.status}`}; }
  return {...j, status: r.status};
}
function say(text, style) {
  const t = $("#toast"); t.textContent = text; t.hidden = !text; t.className = "toast " + (style || "");
  clearTimeout(say.timer); say.timer = setTimeout(() => { t.hidden = true; }, 4000);
  $("#footer").textContent = text;
}
function ago(iso) {
  const t = Date.parse(iso); if (!t) return "";
  const s = Math.max(0, (Date.now() - t) / 1000);
  return s < 90 ? "just now" : s < 5400 ? `${Math.round(s / 60)} min ago` : s < 129600 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} d ago`;
}
// A small markdown block: paragraphs, bullets, bold, code, tables. No library.
function md(text) {
  const lines = String(text || "").split("\n"); let out = "", list = false, table = false;
  const inl = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, "<code>$1</code>");
  for (const ln of lines) {
    if (/^\s*[-*] /.test(ln)) { if (!list) { out += "<ul>"; list = true; } out += `<li>${inl(ln.replace(/^\s*[-*] /, ""))}</li>`; continue; }
    if (list) { out += "</ul>"; list = false; }
    if (/^\s*\|/.test(ln)) {
      if (/^\s*\|[\s:|-]+\|\s*$/.test(ln)) continue;
      if (!table) { out += "<table>"; table = true; }
      out += "<tr>" + ln.trim().replace(/^\||\|$/g, "").split("|").map(c => `<td>${inl(c.trim())}</td>`).join("") + "</tr>"; continue;
    }
    if (table) { out += "</table>"; table = false; }
    if (/^#+ /.test(ln)) out += `<p><b>${inl(ln.replace(/^#+ /, ""))}</b></p>`;
    else if (ln.trim()) out += `<p>${inl(ln)}</p>`;
  }
  return out + (list ? "</ul>" : "") + (table ? "</table>" : "");
}

// ============================================================ screens
function show(screen) {
  if (!["home", "questions", "answers", "run", "terminal"].includes(screen)) screen = "home";
  S.screen = screen;
  $$(".screen").forEach(s => { s.hidden = s.id !== "s-" + screen; });
  $$("#nav button").forEach(b => b.classList.toggle("on", b.dataset.screen === screen));
  if (!location.hash.startsWith("#" + screen)) history.replaceState(null, "", "#" + screen);
  const ack = {questions: "questions", run: "run_these"}[screen];
  if (ack && S.desk && S.desk.attention.key === ack) post("/api/do", {attention_ack: true, key: ack});
  if (screen === "questions") loadQuestions();
  if (screen === "answers") loadAnswers();
  if (screen === "run") drawRun();
  if (screen === "terminal") xtEnter();
  if (screen === "home") orbMount();
}
$$("#nav button").forEach(b => b.onclick = () => show(b.dataset.screen));

// ============================================================ the poll
async function poll() {
  try {
    const d = await getJ("/api/desk");
    S.desk = d; drawHome(d);
    $("#live").textContent = "live · " + new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", second: "2-digit"});
    if (d.goto && d.goto.id && d.goto.id !== S.lastGoto) { S.lastGoto = d.goto.id; show(d.goto.screen); post("/api/do", {goto_seen: d.goto.id}); }
    if (d.counts.pins && !S.pinHidden && $("#pinmodal").hidden) loadPins();
    if (S.screen === "run") drawRun();
  } catch (e) { $("#live").textContent = TOKEN ? "the desk is not answering" : "no token: open Polaris with bin/polaris"; }
}
function drawHome(d) {
  const st = d.status || {}, ag = st.agents || {};
  $("#examplebar").hidden = !d.example;
  $("#nQ").textContent = d.counts.questions || ""; $("#nR").textContent = d.counts.run_these || "";
  $("#agN").textContent = `${(ag.running || []).length} running`;
  $("#agents").innerHTML = (ag.running || []).map(a => `<div class="row"><span class="pill amber">running</span>${esc(a.title || a.id)} <span class="dim">${esc(a.model || "")} · ${esc(ago(a.started))}</span><div class="dim">${esc(a.doing || "")}</div></div>`).join("")
    + (ag.finished || []).slice(0, 5).map(a => `<div class="row"><span class="pill ${["failed", "dead", "timed-out", "crashed", "orphaned"].includes(a.status) ? "red" : "green"}">${esc(a.status)}</span>${esc(a.title || a.id)} <span class="dim">${esc(ago(a.ended))}</span></div>`).join("")
    || `<div class="dim">no agents in the feed</div>`;
  const jobs = st.jobs || [];
  $("#jobN").textContent = jobs.length ? `${jobs.filter(j => j.status === "ok").length} of ${jobs.length} ok` : "";
  $("#jobs").innerHTML = jobs.map(j => `<div class="row"><span class="pill ${j.status === "ok" ? "green" : j.status === "failed" || j.status === "died" ? "red" : "amber"}">${esc(j.status)}</span>${esc(j.label)} <span class="dim">${esc(j.when || "")}</span></div>`).join("") || `<div class="dim">no jobs in the feed</div>`;
  $("#headline").textContent = st.headline || "";
  $("#qN").textContent = d.counts.questions ? `${d.counts.questions} waiting` : "";
  const q = d.top_question;
  $("#topq").innerHTML = q ? `<div>${esc(q.question)}</div><div class="dim">from ${esc(q.from)}</div><div class="opts">${(q.options || []).map((o, i) => `<button data-opt="${i}"><span class="k">${i + 1}</span>${esc(o)}</button>`).join("")}<button data-open="1">Open</button></div>` : `<div class="dim">nothing waiting on you</div>`;
  $$("#topq [data-opt]").forEach(b => b.onclick = () => answerQ(q.id, q.options[+b.dataset.opt]));
  const o = $("#topq [data-open]"); if (o) o.onclick = () => show("questions");
  $("#rN").textContent = d.counts.run_these ? `${d.counts.run_these} open` : "";
  $("#homeR").hidden = !d.counts.run_these;
  $("#homeRun").innerHTML = (d.run_these || []).slice(0, 3).map(c => `<div class="row">${verdictPill(c)} ${esc(c.title)}</div>`).join("") + (d.counts.run_these ? `<div class="row-btns"><button class="btn" id="goRun">Open Run these</button></div>` : "");
  const g = $("#goRun"); if (g) g.onclick = () => show("run");
  $("#errors").textContent = (d.errors || []).join(" · ") || "none";
  const a = d.attention || {};
  $("#why").textContent = a.on ? a.why : ""; $("#why").classList.toggle("critical", !!a.critical);
  $("#stateline").textContent = `core · ${d.state} · ${d.counts.running} agent${d.counts.running === 1 ? "" : "s"} · source ${d.source}`;
  orbSet();
}
$("#why").onclick = () => { const a = (S.desk || {}).attention || {}; if (a.on) post("/api/do", {attention_ack: true, key: a.key}).then(poll); };

// ============================================================ Questions
async function loadQuestions() {
  const d = await getJ("/api/mod/questions"); S.q = d; drawQuestions();
}
function drawQuestions() {
  const d = S.q || {items: []}, it = d.items[0];
  $("#qLater").textContent = d.later ? `· ${d.later} asked again later` : "";
  if (!it) { $("#qcard").innerHTML = `<div class="qbig"><div class="q">Nothing is waiting on your answer.</div></div>`; $("#qrest").innerHTML = ""; return; }
  $("#qcard").innerHTML = `<div class="qbig"><div class="label">FROM ${esc(it.from)} · ${esc(ago(it.ts))}${it.skipped ? " · skipped once" : ""}</div>
    <div class="q">${esc(it.question)}</div>${it.context ? `<div class="dim">${esc(it.context)}</div>` : ""}
    <div class="opts">${(it.options || []).map((o, i) => `<button data-opt="${i}"><span class="k">${i + 1}</span>${esc(o)}</button>`).join("")}</div>
    <form id="qform" class="send"><input id="qtext" placeholder="or type an answer, Enter files it" autocomplete="off"><button>File it</button></form>
    <div class="row-btns"><button class="btn" id="qskip">Skip (s)</button><button class="btn" id="qlater">Ask me later (l)</button><button class="btn" id="qdet">Details (d)</button></div>
    <div id="qdetails" class="md" ${S.qDetails ? "" : "hidden"}>${md(it.details)}</div></div>`;
  $$("#qcard [data-opt]").forEach(b => b.onclick = () => answerQ(it.id, it.options[+b.dataset.opt]));
  $("#qform").onsubmit = e => { e.preventDefault(); const v = $("#qtext").value.trim(); if (v) answerQ(it.id, v); };
  $("#qskip").onclick = () => qop("skip", it.id); $("#qlater").onclick = () => qop("later", it.id);
  $("#qdet").onclick = () => { S.qDetails = !S.qDetails; $("#qdetails").hidden = !S.qDetails; };
  $("#qrest").innerHTML = d.items.slice(1).map(x => `<div class="item"><div>${esc(x.question)}</div><div class="dim">from ${esc(x.from)} · ${esc(ago(x.ts))}${x.skipped ? " · skipped" : ""}</div></div>`).join("");
}
async function answerQ(id, text) {
  const r = await post("/api/mod/questions", {op: "answer", id, answer: text});
  say(r.ok ? r.message : r.error, r.ok ? "" : "red"); S.qDetails = false;
  if (S.screen === "questions") loadQuestions(); poll();
}
async function qop(op, id) { const r = await post("/api/mod/questions", {op, id}); say(r.ok ? r.message : r.error); loadQuestions(); poll(); }

// ============================================================ Answers
async function loadAnswers() {
  const d = await getJ("/api/mod/answers");
  $("#answers").innerHTML = (d.items || []).map((a, i) => `<div class="item"><div><b>${esc(a.title)}</b> <span class="dim">${esc(ago(a.when))}</span></div><div class="md" data-i="${i}" hidden>${md(a.body)}</div><button class="btn" data-open="${i}">Read</button></div>`).join("")
    || `<div class="dim">${(d.errors || []).join(" · ") || "nothing has come back yet"}</div>`;
  $$("#answers [data-open]").forEach(b => b.onclick = () => { const m = $(`#answers [data-i="${b.dataset.open}"]`); m.hidden = !m.hidden; });
}

// ============================================================ Run these
function verdictPill(c) {
  const when = c.checked_at ? ` · checked ${ago(c.checked_at)}` : "";
  if (c.verdict === "ok") return `<span class="pill green">safe to paste${when}</span>`;
  if (c.verdict === "stale") return `<span class="pill red">Do not run: ${esc(c.check.replace(/^stale:\s*/, ""))}</span>`;
  if (c.verdict === "unverified") return `<span class="pill amber">not verified: ${esc(c.check.replace(/^ok \(unverified\):\s*/, ""))}${when}</span>`;
  return `<span class="pill amber">Not checked yet</span>`;
}
function drawRun() {
  const rows = (S.desk || {}).run_these || [];
  $("#runlist").innerHTML = rows.map(c => `<div class="item"><div><b>${esc(c.title)}</b> <span class="dim">${esc(ago(c.created))} · from ${esc(c.from)}</span></div>
    <div>${verdictPill(c)}</div>${c.note ? `<div class="md">${md(c.note)}</div>` : ""}
    ${c.verdict === "stale" ? "" : `<code class="cmd">! ${esc(c.command)}</code>`}
    ${c.expect ? `<div class="dim">What you should see: ${esc(c.expect)}</div>` : ""}
    <div class="row-btns">${c.verdict === "stale" ? "" : `<button class="btn primary" data-copy="${esc(c.id)}">Copy</button><button class="btn" data-done="${esc(c.id)}">Done</button>`}<button class="btn" data-drop="${esc(c.id)}">Drop</button></div></div>`).join("")
    || `<div class="dim">nothing waiting for you to run</div>`;
  $$("#runlist [data-copy]").forEach(b => b.onclick = () => { const c = rows.find(x => x.id === b.dataset.copy); copy("! " + c.command); });
  $$("#runlist [data-done]").forEach(b => b.onclick = () => runAct("done", b.dataset.done));
  $$("#runlist [data-drop]").forEach(b => b.onclick = () => runAct("drop", b.dataset.drop));
}
async function runAct(action, id) { const r = await post("/api/do", {run_these: {action, id}}); say(r.ok ? r.message : r.error, r.ok ? "" : "red"); poll(); }
function copy(text) {
  const done = () => say(`copied ${text.length} characters`);
  const fallback = () => { const a = document.createElement("textarea"); a.value = text; document.body.appendChild(a); a.select(); try { document.execCommand("copy"); done(); } catch (e) { say("the copy was refused", "red"); } a.remove(); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback); else fallback();
}

// ============================================================ Pins: the popup on next open
async function loadPins() {
  const d = await getJ("/api/mod/pins"); const p = (d.items || [])[0]; if (!p) return;
  S.pin = p; $("#pinTitle").textContent = p.title; $("#pinBody").innerHTML = md(p.body); $("#pinLink").textContent = p.link || "";
  $("#pinMore").textContent = d.n > 1 ? `· ${d.n - 1} more` : ""; $("#pinmodal").hidden = false; $("#pinOk").focus();
}
$("#pinOk").onclick = async () => { await post("/api/mod/pins", {op: "dismiss", id: S.pin.id}); $("#pinmodal").hidden = true; poll(); };
$("#pinLater").onclick = () => { S.pinHidden = true; $("#pinmodal").hidden = true; };
window.addEventListener("focus", () => { S.pinHidden = false; poll(); });

// ============================================================ the input line
$("#sendform").onsubmit = async e => { e.preventDefault(); const v = $("#sendbox").value.trim(); if (!v) return;
  const r = await post("/api/send", {text: v}); say(r.ok ? r.line : r.error, r.ok ? "" : "red"); if (r.ok) $("#sendbox").value = ""; };
$("#sendbox").addEventListener("focus", () => orbSet()); $("#sendbox").addEventListener("blur", () => orbSet());

// ============================================================ the orb: WebGL2 neural sphere, else a 2D canvas
$$("#chips [data-chip]").forEach(b => b.onclick = () => {
  if (b.dataset.chip === "pause") { S.paused = !S.paused; b.classList.toggle("on", S.paused); if (S.orb) S.paused ? S.orb.pause() : S.orb.resume(); return orbSet(); }
  S.chip = b.dataset.chip; $$("#chips [data-chip]").forEach(x => x.dataset.chip !== "pause" && x.classList.toggle("on", x === b)); orbSet();
});
function orbState() {
  if (document.activeElement === $("#sendbox")) return "listening";
  if (S.chip !== "auto") return S.chip;
  return (S.desk || {}).state || "idle";
}
function loadScript(src) { return new Promise((ok, no) => { const el = document.createElement("script"); el.src = src; el.onload = ok; el.onerror = no; document.head.appendChild(el); }); }
async function orbMount() {
  if (S.orbTried) return;               // mounted once: the 3D orb, or the 2D fallback, keeps running
  S.orbTried = true;
  try {
    await loadScript("/orbs/orb_core.js");
    if (!window.PolarisOrbs || !PolarisOrbs.supported()) throw new Error("no WebGL2");
    await loadScript("/orbs/neural.js");
    S.orb = PolarisOrbs.mount($("#orb3d"), "neural", {bloom: true});
    if (!S.orb) throw new Error("no WebGL2 context");
    $("#orb2d").hidden = true; orbSet();
  } catch (e) { orb2d(); }
}
function orbSet() {
  const st = orbState(), n = ((S.desk || {}).counts || {}).running || 0;
  if (S.orb) S.orb.set({state: st, agents: n});
  S.o2 = {state: st, agents: n};
}
function orb2d() {
  if (S.o2loop) return; S.o2loop = true; $("#orb3d").hidden = true;
  const cv = $("#orb2d"), g = cv.getContext("2d"), pts = [];
  for (let i = 0; i < 700; i++) { const a = Math.random() * Math.PI * 2, r = Math.pow(Math.random(), .6); pts.push([a, r, Math.random()]); }
  const col = {idle: [125, 211, 252], listening: [134, 239, 172], working: [252, 211, 77], error: [248, 113, 113], speaking: [196, 167, 231]};
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let t = 0;
  (function frame() {
    const s = (S.o2 || {}).state || "idle", c = col[s] || col.idle, w = cv.width, h = cv.height;
    g.clearRect(0, 0, w, h);
    const speed = s === "working" ? .006 : s === "error" ? .002 : .003; if (!S.paused && !reduce) t += speed;
    for (const [a, r, k] of pts) {
      const ang = a + t * (1.5 - r), x = w / 2 + Math.cos(ang) * r * w * .42, y = h / 2 + Math.sin(ang) * r * h * .42 * .55;
      g.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${.25 + .6 * k * (1 - r)})`; g.fillRect(x, y, 1.6, 1.6);
    }
    if (!document.hidden && !HEADLESS) requestAnimationFrame(frame);
    else setTimeout(() => requestAnimationFrame(frame), 1000);
  })();
}

// ============================================================ Terminal tabs: real terminals over the desk's ptys (/api/pty/*)
// "shell-N" is your login shell in POLARIS_REPO, "session" is POLARIS_SESSION_CMD, both under tmux
// when tmux is on PATH, so closing Polaris only detaches. One xterm.js per tab; one long-poll
// (/api/pty/reads) carries every tab's output, because a browser holds six connections to a host
// and a poll per tab would starve the rest of the page. The token goes in a header, never a URL.
const XT_TABS = /^(session|shell-[1-9]\d?)$/;
const XT = {mode: XT_TABS.test(store.get("polaris-term") || "") ? store.get("polaris-term") : "shell-1", tabs: {}, order: [], loaded: false, abort: null, pumping: false, info: {}, esc: null};
const XT_KEYS = new Set("hqAr`/".split(""));   // Esc then one of these within half a second leaves the terminal
const XT_THEME = {background: "#05070a", foreground: "#d9dee8", cursor: "#7dd3fc", cursorAccent: "#05070a", selectionBackground: "rgba(125,211,252,.28)",
  black: "#1b2129", red: "#f87171", green: "#86efac", yellow: "#fcd34d", blue: "#7aa2f7", magenta: "#c4a7e7", cyan: "#7dd3fc", white: "#d9dee8",
  brightBlack: "#5b6675", brightRed: "#fca5a5", brightGreen: "#bbf7d0", brightYellow: "#fde68a", brightBlue: "#a5b4fc", brightMagenta: "#ddd6fe", brightCyan: "#a5f3fc", brightWhite: "#ffffff"};
async function xtLoad() {
  if (XT.loaded) return true;
  const css = document.createElement("link"); css.rel = "stylesheet"; css.href = "/vendor/xterm/xterm.css"; document.head.appendChild(css);
  try { for (const src of ["/vendor/xterm/xterm.js", "/vendor/addon-fit/addon-fit.js"]) await loadScript(src); } catch (e) { return false; }
  XT.loaded = !!(window.Terminal && window.FitAddon); return XT.loaded;
}
function xtNote(text, style) { const n = $("#termnote"); n.textContent = text || ""; n.className = "q " + (style || "dim"); }
const xtB64 = s => btoa(unescape(encodeURIComponent(s)));
function xtLabel(id) { return id === "session" ? "session" : `${String(XT.info.shell || "shell").split("/").pop()} ${id.split("-")[1]}`; }
function xtBar() {
  const ids = XT.order.slice(); if (!ids.includes(XT.mode)) ids.push(XT.mode);
  ids.sort((a, b) => (a === "session") - (b === "session") || (+a.split("-")[1] || 0) - (+b.split("-")[1] || 0));
  $("#termtabs").innerHTML = ids.map(id => { const t = XT.tabs[id] || {};
    return `<button data-tmode="${id}" class="${XT.mode === id ? "on" : ""} ${t.open === false ? "dead" : ""}">${esc(xtLabel(id))}<span class="x" data-tclose="${id}" title="${(t.st || {}).tmux ? "detach (the tmux session lives on)" : "close"}">×</span></button>`; }).join("");
  $("#termNewSession").style.display = ids.includes("session") ? "none" : "";
  $$("#termtabs [data-tmode]").forEach(b => b.onclick = e => { const x = e.target.closest("[data-tclose]"); if (x) { e.stopPropagation(); return xtClose(x.dataset.tclose); } termMode(b.dataset.tmode); });
}
function xtStatus() {
  const t = XT.tabs[XT.mode] || {}, st = t.st || {};
  $("#termEnd").style.display = t.open ? "" : "none"; $("#termEnd").textContent = st.tmux ? "Detach" : "End";
  if (t.error) return xtNote(t.error, "red");
  if (!t.open) return xtNote(t.ended || (t.opening ? "opening…" : "not open"), t.opening ? "" : "yellow");
  const keys = t.focused ? "keys go to the terminal · Esc then a Polaris key, or a click outside, gives them back" : "click the terminal to type";
  const where = st.tmux ? `tmux "${st.name}": closing Polaris detaches, the next open reattaches` : "no tmux: closing Polaris ends it";
  xtNote(`${where} · ${keys}`, t.focused ? "green" : "");
}
function xtSend(t, s) {
  if (!t.open || t.replay) return;
  t.out += s; if (t.sending) return;
  t.sending = true;
  (async () => { try { while (t.out) { const d = t.out; t.out = ""; await post("/api/pty/write", {id: t.id, data: xtB64(d)}); } } finally { t.sending = false; } })();
}
function xtKey(t, e) {
  if (e.type !== "keydown") return true;
  if (e.metaKey) {
    if (e.key === "c" && t.term.hasSelection()) { copy(t.term.getSelection()); return false; }
    if (e.key === "k") { t.term.clear(); return false; }
    return false;
  }
  if (["Shift", "Control", "Alt", "Meta", "CapsLock"].includes(e.key)) return true;
  if (XT.esc) {
    clearTimeout(XT.esc.timer); const held = XT.esc; XT.esc = null;
    if (!e.ctrlKey && !e.altKey && XT_KEYS.has(e.key)) { t.term.blur(); return false; }
    xtSend(held.t, "\x1b");
  }
  if (e.key === "Escape" && !e.ctrlKey && !e.altKey && !e.shiftKey) {
    XT.esc = {t, timer: setTimeout(() => { if (XT.esc && XT.esc.t === t) { XT.esc = null; xtSend(t, "\x1b"); } }, 500)};
    return false;
  }
  return true;
}
function xtMake(id) {
  if (XT.tabs[id] && XT.tabs[id].term) return XT.tabs[id];
  const t = XT.tabs[id] = Object.assign(XT.tabs[id] || {}, {id, cursor: 0, out: "", open: false, replay: false});
  t.el = document.createElement("div"); t.el.className = "xt-pane off"; t.el.dataset.tab = id;
  t.el.innerHTML = `<div class="xt-host"></div><div class="xt-end" style="display:none"><div class="why"></div><div><button class="btn primary" data-xt="retry">Retry</button> <button class="btn" data-xt="close">Close tab</button></div></div>`;
  $("#xtermwrap").appendChild(t.el);
  t.term = new Terminal({cursorBlink: true, fontSize: 13, lineHeight: 1.15, scrollback: 10000, allowProposedApi: true, macOptionClickForcesSelection: true,
    rightClickSelectsWord: true, fontFamily: getComputedStyle(document.body).getPropertyValue("--mono").trim() || "monospace", theme: XT_THEME});
  t.fit = new FitAddon.FitAddon(); t.term.loadAddon(t.fit); t.term.open(t.el.querySelector(".xt-host"));
  t.term.attachCustomKeyEventHandler(e => xtKey(t, e));
  t.term.onData(d => xtSend(t, d));
  t.term.onBinary(d => { if (t.open && !t.replay) post("/api/pty/write", {id, data: btoa(d)}); });
  t.term.onResize(({cols, rows}) => t.open && post("/api/pty/resize", {id, cols, rows}));
  // OSC 52: tmux (mouse on) copies a drag-selection by sending it here; it goes to the clipboard.
  t.term.parser.registerOscHandler(52, data => { const b = data.split(";").pop(); if (b && b !== "?") { try { copy(decodeURIComponent(escape(atob(b)))); } catch (e) { /* not base64 */ } } return true; });
  const ta = t.el.querySelector("textarea");
  if (ta) { ta.addEventListener("focus", () => { t.focused = true; if (XT.mode === id) xtStatus(); }); ta.addEventListener("blur", () => { t.focused = false; if (XT.mode === id) xtStatus(); }); }
  t.el.querySelector('[data-xt="retry"]').onclick = () => { t.error = ""; xtOpen(id, true); };
  t.el.querySelector('[data-xt="close"]').onclick = () => xtClose(id);
  return t;
}
function xtEnded(t, why, style) {
  t.open = false; t.ended = why; const box = t.el.querySelector(".xt-end");
  box.style.display = why ? "" : "none"; box.className = "xt-end " + (style || ""); box.querySelector(".why").textContent = why || "";
  if (XT.mode === t.id) { xtStatus(); xtBar(); }
}
function xtFit() { const t = XT.tabs[XT.mode]; if (t && t.fit && S.screen === "terminal") { try { t.fit.fit(); } catch (e) { /* hidden */ } } }
async function xtOpen(id, fresh) {
  if (!(await xtLoad())) return xtNote("xterm.js is missing: run ./setup.sh, which fetches it into web/vendor", "red");
  const t = xtMake(id);
  $$(".xt-pane").forEach(p => p.classList.toggle("off", p.dataset.tab !== id));
  xtFit();
  if (t.open && !fresh) { xtStatus(); xtBar(); t.term.focus(); return xtPump(); }
  t.opening = true; xtEnded(t, ""); xtStatus();
  let st;
  try { st = await getJ(`/api/pty?id=${id}`); } catch (e) { t.opening = false; return xtEnded(t, "the desk did not answer: is Polaris running?", "red"); }
  XT.info = st;
  if (!st.open) st = await post("/api/pty/open", {id, cols: t.term.cols, rows: t.term.rows});
  t.opening = false;
  if (!st.ok && st.status !== 409) { t.st = st; return xtEnded(t, st.error || "could not open this terminal", "red"); }
  if (st.status === 409) st = await getJ(`/api/pty?id=${id}`);
  t.st = st; t.open = true; if (!XT.order.includes(id)) XT.order.push(id);
  if (st.cursor > 0) {
    // Back to a running terminal. Under tmux, skip the old bytes and let a resize make tmux redraw;
    // without tmux, replay what was kept with the terminal's answers switched off: a replayed
    // "who are you" query from tmux, answered again, lands in the shell as typed text.
    t.term.reset();
    if (st.tmux) t.cursor = st.cursor;
    else { t.cursor = Math.max(0, st.cursor - 131072); t.replay = true; }
    await post("/api/pty/resize", {id, cols: t.term.cols, rows: Math.max(2, t.term.rows - 1)});
    await post("/api/pty/resize", {id, cols: t.term.cols, rows: t.term.rows});
  } else { t.cursor = 0; t.term.reset(); }
  xtBar(); xtStatus(); if (XT.mode === id) t.term.focus(); xtPump(true);
}
async function xtPump(restart) {
  if (restart && XT.abort) XT.abort.abort();
  if (XT.pumping && !restart) return;
  const me = XT.pumpGen = (XT.pumpGen || 0) + 1; XT.pumping = true;
  let rounds = 0;
  try {
    while (me === XT.pumpGen && S.screen === "terminal") {
      const open = Object.values(XT.tabs).filter(t => t.open);
      if (!open.length) break;
      XT.abort = new AbortController();
      let r;
      try { r = await getJ(`/api/pty/reads?c=${open.map(t => `${t.id}:${t.cursor}`).join(",")}&wait=${HEADLESS ? 1 : 25}`, XT.abort.signal); }
      catch (e) { if (me !== XT.pumpGen) return; await new Promise(ok => setTimeout(ok, 1000)); continue; }
      if (me !== XT.pumpGen) return;
      for (const [id, c] of Object.entries(r.tabs || {})) {
        const t = XT.tabs[id]; if (!t || !t.open) continue;
        if (c.data) { const bytes = Uint8Array.from(atob(c.data), ch => ch.charCodeAt(0)); if (t.replay) t.term.write(bytes, () => { t.replay = false; }); else t.term.write(bytes); }
        else if (t.replay) t.replay = false;
        if (c.cursor != null) t.cursor = c.cursor;
        if (!c.open) { t.st = {...(t.st || {}), open: false, exit: c.exit}; xtEnded(t, c.missing ? "the desk restarted: this terminal is gone" : c.ended || `ended (exit ${c.exit})`, c.exit ? "red" : "yellow"); }
      }
      if (HEADLESS && ++rounds > 4) break;
    }
  } finally { if (me === XT.pumpGen) XT.pumping = false; }
}
async function xtClose(id) {
  const t = XT.tabs[id];
  if (t && t.open) { const r = await post("/api/pty/close", {id}); say(r.tmux ? `detached from tmux "${(t.st || {}).name}": it lives on` : `${xtLabel(id)} ended`); }
  if (t) { t.open = false; t.term && t.term.dispose(); t.el && t.el.remove(); delete XT.tabs[id]; }
  XT.order = XT.order.filter(x => x !== id);
  if (XT.mode === id) termMode(XT.order[XT.order.length - 1] || "shell-1"); else xtBar();
}
function xtNextShell() { for (let n = 1; n < 10; n++) if (!XT.order.includes(`shell-${n}`)) return `shell-${n}`; return "shell-9"; }
async function xtEnter() {
  try { const st = await getJ(`/api/pty?id=${XT.mode}`); XT.info = st;
    for (const v of st.tabs || []) if (v.open && !XT.order.includes(v.id)) XT.order.push(v.id); } catch (e) { /* the tab says so when it opens */ }
  termMode(XT.mode);
}
function termMode(m) { XT.mode = m; store.set("polaris-term", m); xtBar(); return xtOpen(m); }
$("#termNewShell").onclick = () => termMode(xtNextShell());
$("#termNewSession").onclick = () => termMode("session");
$("#termEnd").onclick = () => xtClose(XT.mode);
new ResizeObserver(() => xtFit()).observe($("#xtermwrap"));

// ============================================================ keys
document.addEventListener("keydown", e => {
  if (!$("#pinmodal").hidden) {
    if (e.key === "Enter") { e.preventDefault(); $("#pinOk").click(); }
    if (e.key === "Escape") { e.preventDefault(); $("#pinLater").click(); }
    return;
  }
  const typing = /^(INPUT|TEXTAREA)$/.test((document.activeElement || {}).tagName || "");
  if (typing) { if (e.key === "Escape") document.activeElement.blur(); return; }
  if (e.metaKey || e.ctrlKey || e.altKey) return;
  const go = {h: "home", q: "questions", A: "answers", r: "run", "`": "terminal"}[e.key];
  if (go) { e.preventDefault(); return show(go); }
  if (e.key === "/") { e.preventDefault(); show("home"); return $("#sendbox").focus(); }
  if (S.screen === "questions" && S.q && S.q.items[0]) {
    const it = S.q.items[0];
    if (/^[1-9]$/.test(e.key) && it.options[+e.key - 1]) return answerQ(it.id, it.options[+e.key - 1]);
    if (e.key === "s") return qop("skip", it.id);
    if (e.key === "l") return qop("later", it.id);
    if (e.key === "d") return $("#qdet").click();
    if (e.key === "i") { e.preventDefault(); return $("#qtext").focus(); }
  }
});

function tick() { $("#clock").textContent = new Date().toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"}); }
tick(); setInterval(tick, 10000);
show((location.hash || "#home").slice(1).split("/")[0]);
poll(); setInterval(poll, 3000);
