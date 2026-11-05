/* JARVIS console — talks to jarvis/server.py.
 *
 * Flow: POST /api/runs starts a skill -> EventSource follows /api/runs/<id>/events ->
 * every event goes through handle(), which updates the core, the dialogue and the telemetry.
 * Add a handler in HANDLERS for any new event type a future skill emits.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const els = {
  body: document.body, feed: $("feed"), intro: $("intro"), question: $("question"), send: $("send"), mic: $("mic"),
  options: $("options"), skills: $("skills"), systems: $("systems"), history: $("history"), stages: $("stages"),
  evidence: $("evidence"), evidenceMeta: $("evidence-meta"), log: $("log"), cancel: $("cancel"), conn: $("conn"),
  banner: $("state-banner"), coreState: $("core-state"), coreSub: $("core-sub"), speak: $("speak"), lang: $("voice-lang"),
  autoscroll: $("autoscroll"), toast: $("toast"), boot: $("boot"), bootLines: $("boot-lines"),
};
const state = { skills: [], skill: null, runId: null, source: null, card: null, stageStart: {}, live: false,
                lastAnswer: null, ended: true, recorder: null };

/* ---------------- utilities ---------------- */
function el(tag, props = {}, ...children) {   // DOM builder; text is always set as text, never as HTML
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false)
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  return node;
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}
const store = {   // per-browser preferences; storage may be unavailable, so everything is optional
  get(key, fallback) { try { const v = localStorage.getItem("jarvis." + key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; } },
  set(key, value) { try { localStorage.setItem("jarvis." + key, JSON.stringify(value)); } catch { /* ignore */ } },
};
let toastTimer;
function toast(message, kind = "error") {
  els.toast.textContent = message;
  els.toast.className = "toast " + (kind === "info" ? "info" : "");
  els.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { els.toast.hidden = true; }, 6000);
}
const fmtSeconds = (s) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`);
const hasHebrew = (text) => /[֐-׿]/.test(text);

/* ---------------- core (reactor) ---------------- */
function buildCore() {
  const ns = "http://www.w3.org/2000/svg";
  const ticks = $("ticks");
  for (let i = 0; i < 120; i++) {   // tick ring
    const a = (i / 120) * Math.PI * 2, long = i % 10 === 0, r1 = 150, r2 = long ? 140 : 145;
    const line = document.createElementNS(ns, "line");
    line.setAttribute("x1", 200 + r1 * Math.cos(a)); line.setAttribute("y1", 200 + r1 * Math.sin(a));
    line.setAttribute("x2", 200 + r2 * Math.cos(a)); line.setAttribute("y2", 200 + r2 * Math.sin(a));
    ticks.append(line);
  }
  const segments = $("segments");
  for (let i = 0; i < 10; i++) {   // trapezoid segments around the nucleus
    const a0 = (i / 10) * Math.PI * 2 + 0.06, a1 = ((i + 1) / 10) * Math.PI * 2 - 0.06, ri = 80, ro = 98;
    const p = (r, a) => `${200 + r * Math.cos(a)},${200 + r * Math.sin(a)}`;
    const path = document.createElementNS(ns, "path");
    path.setAttribute("d", `M${p(ri, a0)} L${p(ro, a0)} A${ro},${ro} 0 0 1 ${p(ro, a1)} L${p(ri, a1)} A${ri},${ri} 0 0 0 ${p(ri, a0)}Z`);
    segments.append(path);
  }
}
const STATE_TEXT = {
  idle: ["STANDING BY", "Awaiting instructions"], thinking: ["PROCESSING", "Working"],
  listening: ["LISTENING", "Speak now — I stop when you pause"], transcribing: ["TRANSCRIBING", "Whisper, running locally"],
  speaking: ["RESPONDING", "Voice reply"], error: ["ATTENTION", "Something needs a look"], booting: ["BOOTING", "Initializing"],
};
function setState(name, sub) {
  els.body.dataset.state = name === "transcribing" ? "thinking" : name;
  const [title, defaultSub] = STATE_TEXT[name] || STATE_TEXT.idle;
  els.coreState.textContent = title;
  els.coreSub.textContent = sub || defaultSub;
  els.banner.textContent = sub && name === "thinking" ? `${title} · ${sub.toUpperCase()}` : title;
}

/* ---------------- clock ---------------- */
function tick() {
  const now = new Date();
  $("clock").textContent = now.toLocaleTimeString("en-GB");
  $("date").textContent = now.toLocaleDateString("en-GB", { weekday: "short", day: "2-digit", month: "short" });
}

/* ---------------- systems ---------------- */
async function loadStatus(force = false) {
  const button = $("refresh-status");
  button.classList.add("spin");
  try {
    const data = await api("/api/status" + (force ? "?force=1" : ""));
    renderSystems(data.systems);
    setLink(true);
    return data.systems;
  } catch (error) {
    setLink(false);
    els.systems.replaceChildren(el("li", { class: "muted", text: "Console server unreachable." }));
    return null;
  } finally {
    button.classList.remove("spin");
  }
}
function renderSystems(systems) {
  els.systems.replaceChildren(...Object.values(systems).map((s) => el("li", {},
    el("span", { class: "sys-dot" + (s.ok ? " ok" : ""), title: s.ok ? "online" : "attention" }),
    el("div", {}, el("div", { class: "sys-name", text: s.label }), el("div", { class: "sys-detail", text: s.detail })))));
}
function setLink(ok) {
  els.conn.textContent = ok ? "LINK OK" : "LINK LOST";
  els.conn.className = "pill " + (ok ? "ok" : "bad");
}

/* ---------------- boot ---------------- */
async function boot() {
  const line = (label, value, ok) => els.bootLines.append(el("li", {}, el("span", { text: label }),
    el("span", { class: ok === undefined ? "" : ok ? "ok" : "bad", text: value })));
  const finish = () => { els.boot.classList.add("done"); setState("idle"); setTimeout(() => els.boot.remove(), 700); };
  $("boot-skip").addEventListener("click", finish);
  line("Console link", "…");
  const systems = await loadStatus();
  els.bootLines.lastChild.lastChild.textContent = systems ? "ONLINE" : "FAILED";
  els.bootLines.lastChild.lastChild.className = systems ? "ok" : "bad";
  for (const s of Object.values(systems || {})) {
    await new Promise((r) => setTimeout(r, 140));
    line(s.label, s.ok ? "ONLINE" : "ATTENTION", s.ok);
  }
  await new Promise((r) => setTimeout(r, 650));
  finish();
}

/* ---------------- skills & options ---------------- */
async function loadSkills() {
  state.skills = await api("/api/skills");
  const wanted = store.get("skill", "rag");
  selectSkill(state.skills.find((s) => s.name === wanted) || state.skills[0]);
}
function selectSkill(skill) {
  state.skill = skill;
  store.set("skill", skill.name);
  els.skills.replaceChildren(...state.skills.map((s) => el("button", {
    type: "button", class: "skill", role: "radio", "aria-checked": String(s === skill), onclick: () => selectSkill(s),
  }, el("strong", { text: s.title }), el("small", { text: s.description }))));
  const saved = store.get("options." + skill.name, {});
  els.options.replaceChildren(...skill.options.map((o) => renderOption(o, saved[o.key] ?? o.default)));
  els.question.placeholder = skill.needs_question ? "Command or question…" : "No question needed — press Engage";
}
function renderOption(option, value) {
  const id = "opt-" + option.key;
  if (option.type === "bool")
    return el("label", { class: "toggle" }, el("input", { type: "checkbox", id, checked: !!value, onchange: saveOptions }), el("span"), option.label);
  let input;
  if (option.type === "select")
    input = el("select", { id, onchange: saveOptions }, option.choices.map((c) => el("option", { value: c, selected: c === value, text: c })));
  else if (option.type === "int")
    input = el("input", { id, type: "number", min: option.min, max: option.max, value, onchange: saveOptions });
  else
    input = el("input", { id, type: "text", value: value || "", placeholder: option.placeholder || "", onchange: saveOptions });
  return el("label", { class: "field" }, option.label, input);
}
function readOptions() {
  const values = {};
  for (const o of state.skill.options) {
    const input = $("opt-" + o.key);
    values[o.key] = o.type === "bool" ? input.checked : o.type === "int" ? Number(input.value) : input.value;
  }
  return values;
}
function saveOptions() { store.set("options." + state.skill.name, readOptions()); }

/* ---------------- history ---------------- */
async function loadHistory() {
  try {
    const data = await api("/api/runs");
    els.history.replaceChildren(...data.runs.map((run) => el("li", {}, el("button", {
      type: "button", class: run.id === state.runId ? "current" : "", onclick: () => openRun(run.id),
    }, el("span", { class: "q", dir: "auto", text: run.question || run.headline || run.skill }),
       el("span", { class: "m" }, el("span", { class: "st-" + run.status, text: run.status.toUpperCase() }),
         el("span", { text: new Date(run.started * 1000).toLocaleString("en-GB", { dateStyle: "short", timeStyle: "short" }) }),
         el("span", { text: fmtSeconds(run.seconds) }))))));
    if (!data.runs.length) els.history.replaceChildren(el("li", { class: "muted", text: "No missions yet." }));
    return data;
  } catch { return null; }
}
async function openRun(id) {   // replay a finished run, or follow one that is still running
  try {
    const data = await api(`/api/runs/${encodeURIComponent(id)}`);
    if (data.summary.status === "running") return follow(id);
    closeStream();
    resetTelemetry();
    els.feed.replaceChildren();
    state.runId = id; state.live = false;
    for (const event of data.events) handle(event);
    loadHistory();
  } catch (error) { toast(error.message); }
}

/* ---------------- runs ---------------- */
async function submit(event) {
  event?.preventDefault();
  if (!state.ended) return toast("A run is already in progress.", "info");
  const question = els.question.value.trim();
  if (state.skill.needs_question && !question) return els.question.focus();
  window.speechSynthesis?.cancel();
  els.send.disabled = true;
  try {
    const { run_id } = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill: state.skill.name, request: { question, ...readOptions() } }),
    });
    els.question.value = "";
    autosize();
    follow(run_id);
  } catch (error) {
    toast(error.message);
    els.send.disabled = false;
  }
}
function follow(runId) {
  closeStream();
  resetTelemetry();
  state.runId = runId; state.live = true; state.ended = false;
  els.send.disabled = true;
  const source = new EventSource(`/api/runs/${encodeURIComponent(runId)}/events`);
  state.source = source;
  source.onmessage = (message) => { setLink(true); handle(JSON.parse(message.data)); };
  source.onerror = () => { if (!state.ended) setLink(false); };   // EventSource reconnects by itself with Last-Event-ID
}
function closeStream() { state.source?.close(); state.source = null; }
async function cancelRun() {
  if (!state.runId) return;
  try { await api(`/api/runs/${encodeURIComponent(state.runId)}/cancel`, { method: "POST" }); }
  catch (error) { toast(error.message); }
}

/* ---------------- event handling ---------------- */
const HANDLERS = {
  run_start(e) {
    els.intro?.remove();
    if (e.request.question) els.feed.append(el("div", { class: "msg-user", dir: "auto" }, el("span", { class: "who", text: "YOU" }), e.request.question));
    state.card = el("div", { class: "msg-ai" }, el("span", { class: "who", text: "JARVIS" }),
      el("div", { class: "pending" }, el("i"), el("i"), el("i"), el("span", { text: "Working on it" })));
    els.feed.append(state.card);
    scrollFeed();
    els.stages.replaceChildren(...e.stages.map((s, i) => el("li", { class: "stage", id: "stage-" + s.id },
      el("span", { class: "node" }, el("b", { text: String(i + 1) })), el("span", { class: "name", text: s.title }), el("span", { class: "time", text: "—" }))));
    els.cancel.hidden = !state.live;
    if (state.live) setState("thinking", "Starting");
    logLine(`■ ${e.skill.toUpperCase()} run ${e.run_id}`, "l-stage");
  },
  stage_start(e) {
    const node = $("stage-" + e.stage);
    node?.classList.add("active");
    state.stageStart[e.stage] = e.time;
    if (state.live) setState("thinking", e.title);
    logLine(`▸ ${e.title}`, "l-stage");
  },
  log(e) { logLine(e.line, /STOP|ERROR|Traceback|failed/i.test(e.line) ? "l-err" : ""); },
  stage_end(e) {
    const node = $("stage-" + e.stage);
    if (node) {
      node.classList.remove("active");
      node.classList.add(e.status);
      node.querySelector(".time").textContent = fmtSeconds(e.seconds);
    }
    delete state.stageStart[e.stage];
    logLine(`✓ ${e.stage} ${e.status} in ${fmtSeconds(e.seconds)}`, e.status === "ok" ? "l-ok" : "l-err");
  },
  retrieval(e) {
    const top = Math.max(...e.results.map((r) => r.score), 1e-9);
    els.evidenceMeta.textContent = `${e.method} · ${e.results.length}/${e.candidates ?? "?"}`;
    els.evidence.replaceChildren(...e.results.map((r) => el("div", { class: "ev", title: r.source },
      el("div", { class: "ev-top" }, el("span", { class: "ev-doc" }, `#${r.rank} ${r.document_id}`, el("em", { text: "v" + r.version })),
        el("span", { class: "ev-score", text: r.score.toFixed(4) })),
      el("div", { class: "ev-bar" }, el("i", { style: `width:${Math.max(4, (r.score / top) * 100)}%` })),
      el("div", { class: "ev-meta" }, el("span", { class: r.status === "superseded" ? "superseded" : "", text: r.status }),
        r.semantic_score != null ? el("span", { text: `cos ${r.semantic_score.toFixed(3)}` }) : null,
        r.bm25_score != null ? el("span", { text: `bm25 ${r.bm25_score.toFixed(2)}` }) : null))));
  },
  answer(e) {
    const sources = new Map(e.sources.map((s, i) => [s.chunk_id, { ...s, n: i + 1 }]));
    const claims = el("ul", { class: "claims" }, e.claims.map((claim) => {
      const item = el("li", { class: "claim", dir: "auto" }, claim.text);
      const chips = el("span", { class: "chips" });
      for (const id of claim.citation_ids) {
        const src = sources.get(id);
        const chip = el("button", { type: "button", class: "chip" + (src ? "" : " invalid"), "aria-expanded": "false",
          title: src ? src.metadata.source : "This ID was not among the sources sent to the model",
          text: src ? `S${src.n} ${src.metadata.document_id}` : `✕ ${id.slice(-6)}` });
        chip.addEventListener("click", () => toggleSource(item, chip, src, id));
        chips.append(chip);
      }
      item.append(chips);
      return item;
    }));
    const missing = e.missing_information.length ? el("div", { class: "missing" }, el("h3", { text: "MISSING INFORMATION" }),
      el("ul", {}, e.missing_information.map((m) => el("li", { dir: "auto", text: m })))) : null;
    const statusClass = e.citation_status === "source_ids_valid_not_semantic_proof" ? "ok" : e.invalid_source_ids.length ? "err" : "warn";
    const foot = el("div", { class: "answer-foot" }, el("span", { class: "badge " + statusClass, text: e.citation_status }),
      el("span", { text: `${e.provider} · ${e.model}` }), el("span", { text: `generation ${fmtSeconds(e.seconds)}` }),
      el("span", { text: `${e.sources.length} sources sent` }));
    fillCard(claims, missing, foot);
    state.lastAnswer = e.claims.map((c) => c.text).join(" ");
  },
  message(e) {
    fillCard(el("p", { class: "claim", dir: "auto", text: e.text }));
    state.lastAnswer = e.text;
  },
  status(e) { renderSystems(e.systems); },
  error(e) {
    fillCard(el("p", { class: "claim", text: e.message }));
    state.card?.classList.add("error");
    state.card && (state.card.querySelector(".who").textContent = "JARVIS · ERROR");
    logLine("✕ " + e.message, "l-err");
  },
  run_end(e) {
    state.ended = true;
    closeStream();
    els.cancel.hidden = true;
    els.send.disabled = false;
    for (const node of els.stages.querySelectorAll(".stage.active")) node.classList.replace("active", "cancelled");
    if (e.status === "cancelled") { fillCard(el("p", { class: "claim", text: "Run aborted." })); }
    logLine(`■ ${e.status.toUpperCase()} in ${fmtSeconds(e.seconds)}`, e.status === "done" ? "l-ok" : "l-err");
    const live = state.live;
    state.live = false;
    loadHistory();
    if (!live) return setState("idle");
    if (e.status === "error") { setState("error"); setTimeout(() => els.body.dataset.state === "error" && setState("idle"), 6000); return; }
    if (e.status === "done" && els.speak.checked && state.lastAnswer) speak(state.lastAnswer);
    else setState("idle");
  },
};
function handle(event) {
  const handler = HANDLERS[event.type];
  if (handler) handler(event);
  if (event.type === "run_start") state.lastAnswer = null;
  else if (!state.live) state.ended = true;
}
function fillCard(...nodes) {
  if (!state.card) return;
  state.card.querySelector(".pending")?.remove();
  state.card.append(...nodes.filter(Boolean));
  scrollFeed();
}
function toggleSource(item, chip, src, id) {
  const existing = item.querySelector(`.source[data-id="${CSS.escape(id)}"]`);
  item.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-expanded", "false"));
  item.querySelectorAll(".source").forEach((s) => s.remove());
  if (existing) return;
  chip.setAttribute("aria-expanded", "true");
  const meta = src ? `${src.metadata.source} · ${src.metadata.document_id} v${src.metadata.version} · ${src.metadata.status} · ${id}` : id;
  item.append(el("div", { class: "source", "data-id": id, dir: "auto" },
    src ? src.text : "This chunk was not sent to the model — the citation is invalid.", el("span", { class: "src-meta", text: meta })));
}
function resetTelemetry() {
  els.stages.replaceChildren(el("li", { class: "muted", text: "Starting…" }));
  els.evidence.replaceChildren(el("p", { class: "muted", text: "Retrieved chunks appear here." }));
  els.evidenceMeta.textContent = "";
  els.log.replaceChildren();
  state.stageStart = {};
  state.card = null;
}
function logLine(text, cls = "") {
  if (els.log.childElementCount > 3000) els.log.firstChild.remove();
  els.log.append(el("span", { class: cls, text: text + "\n" }));
  if (els.autoscroll.checked) els.log.scrollTop = els.log.scrollHeight;
}
function scrollFeed() { els.feed.scrollTop = els.feed.scrollHeight; }
setInterval(() => {   // live timers for running stages
  const now = Date.now() / 1000;
  for (const [stage, started] of Object.entries(state.stageStart)) {
    const node = $("stage-" + stage);
    if (node) node.querySelector(".time").textContent = fmtSeconds(Math.max(0, now - started));
  }
}, 200);

/* ---------------- voice out ---------------- */
function speak(text) {
  const synth = window.speechSynthesis;
  if (!synth) return setState("idle");
  const lang = hasHebrew(text) ? "he" : "en";
  const utterance = new SpeechSynthesisUtterance(text.slice(0, 1200));
  const voices = synth.getVoices().filter((v) => v.lang.toLowerCase().startsWith(lang));
  const preferred = lang === "en" ? voices.find((v) => /UK English Male|Daniel|George|Ryan/i.test(v.name)) : null;
  utterance.voice = preferred || voices[0] || null;
  utterance.lang = utterance.voice?.lang || (lang === "he" ? "he-IL" : "en-GB");
  utterance.rate = 1.02;
  utterance.onstart = () => setState("speaking");
  utterance.onend = utterance.onerror = () => { if (els.body.dataset.state === "speaking") setState("idle"); };
  synth.cancel();
  synth.speak(utterance);
}

/* ---------------- voice in: record -> local Whisper ---------------- */
const SILENCE_SECONDS = 1.6, MAX_SECONDS = 60;
async function toggleRecording() {
  if (state.recorder) return state.recorder.stop();
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return toast("This browser cannot record audio.");
  if (!state.ended) return toast("Wait for the current run to finish.", "info");
  let stream;
  try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
  catch { return toast("Microphone permission was denied."); }
  window.speechSynthesis?.cancel();
  const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  const audio = new AudioContext();
  const analyser = audio.createAnalyser();
  analyser.fftSize = 1024;
  audio.createMediaStreamSource(stream).connect(analyser);
  const samples = new Uint8Array(analyser.fftSize);
  const started = performance.now();
  let heard = false, quietSince = null, frame;
  const meter = () => {   // drives the core with the voice level and stops after a pause
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (const s of samples) sum += ((s - 128) / 128) ** 2;
    const level = Math.min(1, Math.sqrt(sum / samples.length) * 6);
    document.documentElement.style.setProperty("--level", level.toFixed(3));
    const now = performance.now();
    if (level > 0.12) { heard = true; quietSince = null; }
    else if (heard) quietSince ??= now;
    if ((heard && quietSince && now - quietSince > SILENCE_SECONDS * 1000) || now - started > MAX_SECONDS * 1000) return recorder.stop();
    frame = requestAnimationFrame(meter);
  };
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = async () => {
    cancelAnimationFrame(frame);
    stream.getTracks().forEach((t) => t.stop());
    audio.close();
    document.documentElement.style.setProperty("--level", "0");
    state.recorder = null;
    els.mic.classList.remove("recording");
    if (!heard) { setState("idle"); return toast("I didn't hear anything.", "info"); }
    await transcribe(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
  };
  state.recorder = recorder;
  recorder.start();
  els.mic.classList.add("recording");
  setState("listening");
  meter();
}
async function transcribe(blob) {
  els.mic.classList.add("busy");
  els.mic.disabled = true;
  setState("transcribing");
  try {
    const language = els.lang.value;
    const result = await api("/api/transcribe" + (language ? "?language=" + language : ""), {
      method: "POST", headers: { "Content-Type": blob.type.split(";")[0] }, body: blob,
    });
    if (!result.text) { setState("idle"); return toast("No speech recognized.", "info"); }
    els.question.value = result.text;
    autosize();
    logLine(`🎙 whisper ${result.model} · ${result.language} · ${result.audio_seconds}s audio in ${result.seconds}s`, "l-dim");
    setState("idle");
    submit();   // speak -> run, like a voice assistant
  } catch (error) {
    setState("idle");
    toast("Transcription failed: " + error.message);
  } finally {
    els.mic.classList.remove("busy");
    els.mic.disabled = false;
  }
}

/* ---------------- composer ---------------- */
function autosize() {
  els.question.style.height = "auto";
  els.question.style.height = Math.min(els.question.scrollHeight, 160) + "px";
}

/* ---------------- start ---------------- */
function init() {
  buildCore();
  tick(); setInterval(tick, 1000);
  els.speak.checked = store.get("speak", true);
  els.lang.value = store.get("lang", "he");
  els.speak.addEventListener("change", () => { store.set("speak", els.speak.checked); if (!els.speak.checked) window.speechSynthesis?.cancel(); });
  els.lang.addEventListener("change", () => store.set("lang", els.lang.value));
  $("composer").addEventListener("submit", submit);
  els.question.addEventListener("input", autosize);
  els.question.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) submit(e); });
  els.mic.addEventListener("click", toggleRecording);
  els.cancel.addEventListener("click", cancelRun);
  $("refresh-status").addEventListener("click", () => loadStatus(true));
  document.addEventListener("keydown", (e) => { if (e.ctrlKey && e.code === "Space") { e.preventDefault(); toggleRecording(); } });
  if (!window.MediaRecorder) { els.mic.disabled = true; els.mic.title = "Recording is not supported in this browser"; }
  window.speechSynthesis?.getVoices();   // some browsers load voices lazily
  loadSkills().catch((error) => toast(error.message));
  const linked = new URLSearchParams(location.hash.slice(1)).get("run");   // deep link: /#run=<id>
  loadHistory().then((data) => { if (data?.active) follow(data.active); else if (linked) openRun(linked); });
  boot();
  setInterval(() => { if (state.ended) loadStatus(); }, 30000);
}
init();
