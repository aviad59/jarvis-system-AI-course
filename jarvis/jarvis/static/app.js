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
  options: $("options"), systems: $("systems"), history: $("history"), stages: $("stages"),
  evidence: $("evidence"), evidenceMeta: $("evidence-meta"), log: $("log"), cancel: $("cancel"), conn: $("conn"),
  banner: $("state-banner"), coreState: $("core-state"), coreSub: $("core-sub"), speak: $("speak"), lang: $("voice-lang"),
  engine: $("stt-engine"), judge: $("judge"),
  autoscroll: $("autoscroll"), toast: $("toast"), boot: $("boot"), bootLines: $("boot-lines"),
};
const state = { conversation: [], skills: [], skill: null, runId: null, source: null, card: null, stageStart: {}, live: false,
                lastAnswer: null, ended: true, recorder: null, recognizer: null, audio: null,
                feedback: null, feedbackIssues: null, judgeTarget: null, judgeButton: null, judgeable: false };
const FEEDBACK = false;   // the Good / Report a problem bar under each answer (and the marks in the Mission log); true = show it

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
  if (name === "speaking") voiceViz.start();
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
  // There is no skill picker: always "Ask Jarvis", which routes every question itself.
  selectSkill(state.skills.find((s) => s.name === "jarvis") || state.skills[0]);
}
function selectSkill(skill) {
  state.skill = skill;
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
         el("span", { text: fmtSeconds(run.seconds) }),
         FEEDBACK && run.feedback ? el("span", { class: "fb-" + run.feedback.rating, title: feedbackText(run.feedback),
           text: run.feedback.rating === "good" ? "▲ GOOD" : "⚑ REPORTED" }) : null)))));
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
    state.runId = id; state.live = false; state.feedback = data.summary.feedback || null;
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
  stopSpeaking();
  els.send.disabled = true;
  try {
    const { run_id } = await api("/api/runs", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill: state.skill.name, request: { question, ...readOptions(), history: state.conversation } }),
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
  state.runId = runId; state.live = true; state.ended = false; state.feedback = null;
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
    closeTheater();
    state.runSkill = e.skill;
    state.judgeable = false;
    if (e.skill === "judge") startJudgeCard(e);   // under the response it grades; never part of the conversation
    else {
      state.asked = e.request.question || "";
      if (e.request.question) els.feed.append(el("div", { class: "msg-user", dir: "auto" }, el("span", { class: "who", text: "YOU" }), e.request.question));
      state.card = el("div", { class: "msg-ai" }, el("span", { class: "who", text: "JARVIS" }),
        el("div", { class: "pending" }, el("i"), el("i"), el("i"), el("span", { text: "Working on it" })));
      els.feed.append(state.card);
    }
    scrollFeed();
    els.stages.replaceChildren(...e.stages.map((s, i) => el("li", { class: "stage", id: "stage-" + s.id },
      el("span", { class: "node" }, el("b", { text: String(i + 1) })), el("span", { class: "name", text: s.title }), el("span", { class: "time", text: "—" }))));
    els.cancel.hidden = !state.live;
    if (state.live) setState("thinking", "Starting");
    logLine(`■ ${e.skill.toUpperCase()} run ${e.run_id}`, "l-stage");
  },
  stages(e) {   // Ask Jarvis: the model picked a tool, so the full pipeline is known now; finished stages keep their state
    const nodes = e.stages.map((s) => $("stage-" + s.id) || el("li", { class: "stage", id: "stage-" + s.id },
      el("span", { class: "node" }, el("b")), el("span", { class: "name", text: s.title }), el("span", { class: "time", text: "—" })));
    nodes.forEach((node, i) => { node.querySelector(".node b").textContent = String(i + 1); });
    els.stages.replaceChildren(...nodes);
  },
  stage_start(e) {
    const node = $("stage-" + e.stage);
    node?.classList.add("active");
    state.stageStart[e.stage] = e.time;
    if (state.live) { setState("thinking", e.title); working(e.title); }
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
    closeChunk();
    els.evidence.replaceChildren(...e.results.map((r) => el("div", { class: "ev", title: "Click to read this chunk", tabindex: "0",
        role: "button", "aria-haspopup": "dialog", onclick: (ev) => openChunk(ev.currentTarget, r),
        onkeydown: (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); openChunk(ev.currentTarget, r); } } },
      el("div", { class: "ev-top" }, el("span", { class: "ev-doc" }, `#${r.rank} ${r.document_id}`, el("em", { text: "v" + r.version })),
        el("span", { class: "ev-score", text: r.score.toFixed(4) })),
      el("div", { class: "ev-bar" }, el("i", { style: `width:${Math.max(4, (r.score / top) * 100)}%` })),
      el("div", { class: "ev-meta" }, el("span", { class: r.status === "superseded" ? "superseded" : "", text: r.status }),
        r.meeting_date ? el("span", { text: r.meeting_date }) : null,
        r.project_id ? el("span", { text: r.project_id }) : null,
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
      el("span", { text: `${e.sources.length} sources sent` }),
      el("button", { type: "button", class: "speak-btn", title: "Read this answer aloud",
        onclick: () => (els.body.dataset.state === "speaking" ? stopSpeaking() : speak(e.claims.map((c) => c.text).join(" "))) }, "🔊 Speak"));
    fillCard(claims, missing, foot);
    state.lastAnswer = e.claims.map((c) => c.text).join(" ");
    state.judgeable = true;
  },
  judgement(e) {   // LLM as a judge: the scorecard of the response it graded (judge.py)
    const mark = (s) => (s == null ? "na" : s >= 4 ? "ok" : s >= 2.5 ? "warn" : "err");
    const verdict = e.overall == null ? "no verdict" : e.overall >= 4.5 ? "strong" : e.overall >= 3.5 ? "good" : e.overall >= 2.5 ? "weak" : "poor";
    const head = el("div", { class: "judge-head" },
      el("span", { class: "judge-score " + mark(e.overall), text: e.overall == null ? "—" : e.overall.toFixed(1) }),
      el("span", { class: "judge-of", text: "/ 5" }), el("span", { class: "judge-verdict " + mark(e.overall), text: verdict }));
    const rows = el("ul", { class: "judge-criteria" }, e.criteria.map((c) => el("li", { class: "jc" },
      el("span", { class: "jc-title", text: c.title }),
      el("span", { class: "jc-dots " + mark(c.score), title: c.score == null ? "not applicable / no verdict" : `${c.score} / 5` },
        c.score == null ? el("em", { text: "n/a" }) : [1, 2, 3, 4, 5].map((n) => el("i", { class: n <= Math.round(c.score) ? "on" : "" })),
        c.score == null ? null : el("b", { text: String(c.score) })),
      el("span", { class: "jc-reason", dir: "auto", text: c.reason || "" }))));
    const marks = { supported: "✓", partial: "◐", unsupported: "✕" };
    const claims = e.claims.length ? el("div", { class: "judge-claims" }, el("h3", { text: "CLAIM BY CLAIM" }),
      el("ul", {}, e.claims.map((c) => el("li", { class: "jcl " + (c.support || "none"), dir: "auto" },
        el("span", { class: "jcl-mark", title: c.support || "no verdict", text: marks[c.support] || "?" }),
        el("span", { class: "jcl-body" }, el("span", { text: c.text }),
          c.cited_ok === false ? el("span", { class: "badge err", text: "wrong citation" }) : null,
          c.reason ? el("span", { class: "jcl-reason", text: c.reason }) : null))))) : null;
    const foot = el("div", { class: "answer-foot" }, el("span", { text: `judged by ${e.provider} · ${e.model}` }),
      e.same_model ? el("span", { class: "badge warn", title: "A model grading its own response tends to be lenient",
        text: "same model as the response" }) : null,
      el("span", { text: `judge ${fmtSeconds(e.seconds)}` }), el("span", { class: "judge-saved", text: "saving…" }));
    fillCard(head, rows, claims, foot);
    if (state.judgeButton?.isConnected) {   // the graded card shows its score; click again to re-judge
      state.judgeButton.textContent = e.overall == null ? "⚖ Judge" : `⚖ ${e.overall.toFixed(1)}/5`;
      state.judgeButton.className = "judge-btn " + mark(e.overall);
    }
  },
  judgement_saved(e) {   // the judgement's copy in MongoDB Atlas (<db>.judgements)
    const note = state.card?.querySelector(".judge-saved");
    if (!note) return;
    note.textContent = e.saved_to ? `saved to ${e.saved_to}` : "not saved to MongoDB";
    note.className = "judge-saved " + (e.saved_to ? "ok" : "warn");
    if (e.error) note.title = e.error;
  },
  route(e) {   // Ask Jarvis: which skill the request went to, and why
    state.card && (state.card.querySelector(".who").textContent = "JARVIS · " + e.title.toUpperCase());
    setState("thinking", e.title);
  },
  message(e) {
    fillCard(el("p", { class: "claim", dir: "auto", text: e.text }));
    say(e.text);
  },
  posts(e) {   // YouTube Shorts from the youtube-shorts MCP server
    const num = (n) => (n == null ? null : Intl.NumberFormat("en", { notation: "compact" }).format(n));
    const date = (d) => (d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : null);
    const posts = e.posts.filter((p) => videoId(p));
    const hebrew = hasHebrew(e.query);
    const single = e.tool === "get_post_details", channel = e.tool === "get_youtube_shorts";
    const what = single ? "Details of the video" : channel
      ? `${e.posts.length} latest Shorts from ${e.query}` : `${e.posts.length} Shorts for “${e.query}”`;
    const head = el("p", { class: "claim", dir: "auto", text: e.posts.length
      ? `${what} (MCP tool: ${e.tool})` : `No Shorts found for “${e.query}”.` });
    if (!posts.length) { fillCard(head); say(head.textContent); return; }
    // Thumbnails in the card; the video itself plays big in the theater (never a link out).
    const gallery = el("div", { class: "gallery" }, posts.map((p, i) => el("button", { type: "button", class: "thumb",
      title: p.title || "", onclick: () => { stopSpeaking(); openTheater(posts, i, true); } },
      el("span", { class: "thumb-img" }, p.thumbnail ? el("img", { src: p.thumbnail, alt: "", loading: "lazy" }) : null,
        el("span", { class: "play-icon", text: "▶" })),
      el("span", { class: "thumb-title", dir: "auto", text: p.title || videoId(p) }))));
    fillCard(head, gallery);
    state.judgeable = true;
    const title = posts[0].title ? `: ${posts[0].title}` : "";
    say(single ? (hebrew ? `הנה הסרטון${title}.` : `Here's the video${title}.`)
      : hebrew ? `מצאתי ${e.posts.length} שורטס ${channel ? "אחרונים מ" : "על "}${e.query}. הנה הראשון${title}.`
      : `I found ${e.posts.length} ${channel ? "latest Shorts from" : "Shorts for"} ${e.query}. Here's the first one${title}.`);
    state.pendingVideo = () => openTheater(posts, 0, true);   // played after the spoken answer (run_end)
  },
  meme(e) {   // a meme from the memes MCP server: Memegen's URL, or /media/... when drawn locally (Hebrew)
    const head = el("p", { class: "claim", dir: "auto", text: `${e.name} · ${e.text.join(" / ")}` });
    const image = el("a", { class: "meme", href: e.image, target: "_blank", rel: "noopener", title: "Open the full image" },
      el("img", { src: e.image, alt: e.text.join(" / "), dir: "auto" }));
    const foot = el("div", { class: "answer-foot" }, el("span", { text: `template ${e.template}` }),
      el("span", { text: e.renderer === "local" ? "drawn locally (Hebrew)" : "Memegen" }), el("span", { text: "MCP tool: generate_meme" }),
      e.notes?.length ? el("span", { dir: "auto", text: `from your notes: ${e.notes.join(", ")}` }) : null);
    fillCard(head, image, foot);
    state.judgeable = true;
    say(e.hebrew ? "הנה המם שלך." : "Here's your meme.");   // after the notes answer, when there is one
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
    state.card?.querySelector(".pending")?.remove();
    if (e.status === "cancelled") { fillCard(el("p", { class: "claim", text: "Run aborted." })); }
    if (e.status === "done" && state.judgeable && state.card) addJudgeButton(state.card, state.runId);
    if (FEEDBACK && state.card && !["vault_sync", "judge"].includes(state.runSkill)) { state.card.append(feedbackBar(state.runId, e.seconds, state.feedback)); scrollFeed(); }
    logLine(`■ ${e.status.toUpperCase()} in ${fmtSeconds(e.seconds)}`, e.status === "done" ? "l-ok" : "l-err");
    const live = state.live;
    state.live = false;
    loadHistory();
    if (live && e.status === "done" && state.asked && state.lastAnswer)   // remembered for the next question (last 6 turns)
      state.conversation = [...state.conversation, { question: state.asked, answer: state.lastAnswer }].slice(-6);
    if (!live) return setState("idle");
    if (e.status === "error") { setState("error"); setTimeout(() => els.body.dataset.state === "error" && setState("idle"), 6000); return; }
    const video = e.status === "done" ? state.pendingVideo : null;   // Shorts: speak the answer first, then play the video
    const reply = state.reply;   // the spoken answer already started when the answer arrived (see handle)
    state.pendingVideo = state.reply = null;
    if (reply) {
      const run = state.runId;
      if (video) reply.then(() => { if (state.runId === run) video(); });
    } else {
      setState("idle");
      video?.();
    }
  },
};
const SPOKEN = new Set(["answer", "message", "posts", "meme"]);   // events that carry the reply
function handle(event) {
  const handler = HANDLERS[event.type];
  if (handler) handler(event);
  if (event.type === "run_start") { state.lastAnswer = null; state.pendingVideo = null; state.reply = null; state.spokenUpTo = 0; }
  // Speak the moment the answer exists, while the run is still wrapping up; never for replayed history.
  // A later part of the same reply (notes answer, then the Shorts line) is queued after it, never restarting it.
  if (SPOKEN.has(event.type) && state.live && els.speak.checked && state.lastAnswer) {
    const fresh = state.lastAnswer.slice(state.spokenUpTo || 0).trim(), run = state.runId;
    state.spokenUpTo = state.lastAnswer.length;
    if (fresh) state.reply = state.reply ? state.reply.then(() => (state.runId === run ? speak(fresh) : null)) : speak(fresh);
  }
  else if (!state.live) state.ended = true;
}
/* Judge: an LLM grades a finished response (skills.JudgeSkill). The button is on the card; the scorecard is in the
   left panel's Judge section, and the judgement is saved in MongoDB. */
function addJudgeButton(card, runId) {
  let foot = [...card.querySelectorAll(":scope > .answer-foot")].pop();
  if (!foot) { foot = el("div", { class: "answer-foot" }); card.append(foot); }   // Shorts cards have no footer of their own
  const button = el("button", { type: "button", class: "judge-btn", title: "Have the model that answered grade this response",
    onclick: () => judgeRun(runId, card, button) }, "⚖ Judge");
  foot.append(button);
}
async function judgeRun(runId, card, button) {
  if (!state.ended) return toast("A run is already in progress.", "info");
  stopSpeaking();
  state.judgeTarget = card; state.judgeButton = button;
  try {
    const { run_id } = await api("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ skill: "judge", request: { run_id: runId } }) });
    follow(run_id);
  } catch (error) { state.judgeTarget = state.judgeButton = null; toast(error.message); }
}
function startJudgeCard(e) {   // the judge's scorecard goes in the left panel; the graded card stays where it is
  state.asked = "";
  const target = state.judgeTarget?.isConnected ? state.judgeTarget : null;
  state.judgeTarget = null;
  if (!target) state.judgeButton = null;
  document.querySelectorAll(".msg-ai.judged").forEach((card) => card.classList.remove("judged"));
  target?.classList.add("judged");
  const question = (e.request.question || "").replace(/^Judge: /, "");
  state.card = el("div", { class: "judge" },   // two children (heading, pending), like a dialogue card: see working()
    el("div", { class: "judge-top" },
      el("div", { class: "judge-what" }, el("span", { class: "who", text: "⚖ JUDGING" }),
        target ? el("button", { type: "button", class: "judge-link", title: "Show the graded response",
          onclick: () => target.scrollIntoView({ behavior: "smooth", block: "center" }) }, "show ↗") : null),
      el("p", { class: "judge-q", dir: "auto", text: question })),
    el("div", { class: "pending" }, el("i"), el("i"), el("i"), el("span", { text: "Judging" })));
  els.judge.replaceChildren(state.card);
  if (!target && !state.live) els.feed.append(el("div", { class: "msg-ai" }, el("span", { class: "who", text: "JARVIS · JUDGE" }),
    el("p", { class: "claim", dir: "auto", text: `Judged: ${question} (the scorecard is in the Judge panel)` })));
}
/* Feedback: rate the response, or tick what went wrong (slow, wrong behavior, ...). Saved with the run. */
function feedbackText(fb) {
  if (fb.rating === "good") return "Rated good" + (fb.comment ? `: ${fb.comment}` : "");
  const labels = Object.fromEntries((state.feedbackIssues || []).map((i) => [i.id, i.label]));
  return "Reported: " + [...fb.issues.map((id) => labels[id] || id), fb.comment].filter(Boolean).join(" · ");
}
async function loadFeedbackIssues() {
  if (!state.feedbackIssues) state.feedbackIssues = (await api("/api/feedback")).issues;
  return state.feedbackIssues;
}
function feedbackBar(runId, seconds, existing) {   // under each finished answer: Good / Report a problem -> tick boxes + comment
  const box = el("div", { class: "feedback" });
  const send = async (body, button) => {
    if (button) button.disabled = true;
    try {
      const { feedback } = await api(`/api/runs/${encodeURIComponent(runId)}/feedback`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      existing = feedback;
      if (state.runId === runId) state.feedback = feedback;
      showDone();
      loadHistory();
      toast(feedback.rating === "good" ? "Thanks for the feedback." : "Report saved. Thanks, it helps improve Jarvis.", "info");
    } catch (error) { toast(error.message); if (button) button.disabled = false; }
  };
  const showAsk = () => box.replaceChildren(el("span", { class: "fb-q", text: "How was this response?" }),
    el("button", { type: "button", class: "fb-btn good", title: "Good response", onclick: (ev) => send({ rating: "good" }, ev.currentTarget) }, "▲ Good"),
    el("button", { type: "button", class: "fb-btn bad", title: "Report a problem with this response", onclick: () => showForm(null) }, "⚑ Report a problem"));
  const showDone = () => box.replaceChildren(
    el("span", { class: "fb-done fb-" + existing.rating, dir: "auto", text: feedbackText(existing) }),
    el("button", { type: "button", class: "fb-btn", onclick: () => showForm(existing.rating === "bad" ? existing : null) }, "Change"));
  async function showForm(prev) {
    let issues;
    try { issues = await loadFeedbackIssues(); } catch (error) { return toast(error.message); }
    const ticked = new Set(prev?.issues || []);
    const comment = el("textarea", { class: "fb-comment", rows: "2", maxlength: "1000", dir: "auto",
      placeholder: "What happened? What did you expect? (optional)" });
    comment.value = prev?.comment || "";
    const boxes = issues.map((i) => el("input", { type: "checkbox", value: i.id, checked: ticked.has(i.id) }));
    const submitBtn = el("button", { type: "submit", class: "fb-btn bad" }, "Send report");
    const form = el("form", { class: "fb-form", onsubmit: (ev) => {
      ev.preventDefault();
      const chosen = boxes.filter((b) => b.checked).map((b) => b.value);
      if (!chosen.length && !comment.value.trim()) return toast("Tick at least one problem, or write a comment.", "info");
      send({ rating: "bad", issues: chosen, comment: comment.value }, submitBtn);
    } },
      el("div", { class: "fb-head" }, el("span", { class: "fb-q", text: "What was wrong with this response?" }),
        seconds != null ? el("span", { class: "fb-time", text: `took ${fmtSeconds(seconds)}` }) : null),
      el("div", { class: "fb-issues" }, issues.map((i, n) => el("label", { class: "fb-issue" }, boxes[n], el("span", { text: i.label })))),
      comment,
      el("div", { class: "fb-actions" }, submitBtn,
        el("button", { type: "button", class: "fb-btn", onclick: () => (existing ? showDone() : showAsk()) }, "Cancel")));
    box.replaceChildren(form);
    scrollFeed();
  }
  if (existing) showDone(); else showAsk();
  return box;
}
function say(text) {   // what is read aloud at the end of the run; a combined run says the answer, then the Shorts line
  state.lastAnswer = [state.lastAnswer, text].filter(Boolean).join(" ");
}
/* Theater: the Short plays large over the center column; the other results are a side list. Esc or ✕ closes it. */
function closeTheater() { document.querySelector(".theater")?.remove(); }
function openTheater(posts, start, autoplay) {
  closeTheater();
  const num = (n) => (n == null ? null : Intl.NumberFormat("en", { notation: "compact" }).format(n));
  const date = (d) => (d ? `${d.slice(0, 4)}-${d.slice(4, 6)}-${d.slice(6, 8)}` : null);
  const player = el("div", { class: "player" });
  const caption = el("div", { class: "player-caption", dir: "auto" });
  const list = el("ul", { class: "posts" });
  const show = (p, play) => {
    player.replaceChildren(play
      ? el("iframe", { src: `https://www.youtube-nocookie.com/embed/${videoId(p)}?autoplay=1&playsinline=1&rel=0`,
          title: p.title || "YouTube Short", allow: "autoplay; encrypted-media; picture-in-picture; fullscreen",
          allowfullscreen: "", referrerpolicy: "strict-origin-when-cross-origin" })
      : el("button", { type: "button", class: "poster", title: "Play", onclick: () => show(p, true) },
          p.thumbnail ? el("img", { src: p.thumbnail, alt: "" }) : null, el("span", { class: "play-icon", text: "▶" })));
    caption.replaceChildren(el("b", { text: p.title || "" }), el("div", { class: "post-meta" }, ...[p.uploader,
      num(p.views) && `${num(p.views)} views`, num(p.likes) && `${num(p.likes)} likes`,
      num(p.comments) && `${num(p.comments)} comments`, date(p.upload_date)].filter(Boolean).map((t) => el("span", { text: t }))));
    for (const item of list.children) item.classList.toggle("current", item.dataset.id === videoId(p));
  };
  list.append(...posts.map((p) => el("li", { "data-id": videoId(p) },
    el("button", { type: "button", class: "post", title: p.title || "", onclick: () => { stopSpeaking(); show(p, true); } },
      p.thumbnail ? el("img", { src: p.thumbnail, alt: "", loading: "lazy" }) : null,
      el("span", { dir: "auto", text: p.title || videoId(p) })))));
  const box = el("div", { class: "theater", role: "dialog", "aria-label": "YouTube Short" },
    el("button", { type: "button", class: "theater-close", title: "Close (Esc)", onclick: closeTheater, text: "✕" }),
    el("div", { class: "theater-main" }, player, caption), posts.length > 1 ? list : null);
  document.querySelector(".center").append(box);
  show(posts[start], autoplay);
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeTheater(); });
function videoId(post) {   // YouTube video ID from the MCP result (id, or the /shorts/ or ?v= URL)
  if (/^[\w-]{11}$/.test(post.id || "")) return post.id;
  const match = /(?:shorts\/|[?&]v=|youtu\.be\/)([\w-]{11})/.exec(post.url || "");
  return match ? match[1] : null;
}
function working(title) {   // the card shows Jarvis is still busy, also after a first result (notes answer -> Shorts search)
  if (!state.card || state.ended) return;
  let pending = state.card.querySelector(".pending");
  if (!pending) {
    pending = el("div", { class: "pending" }, el("i"), el("i"), el("i"), el("span"));
    state.card.append(pending);
    scrollFeed();
  }
  pending.querySelector("span").textContent = state.card.childElementCount > 2 ? `Still working · ${title}` : title;
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
  const m = src?.metadata;
  const meta = src ? [m.source, `${m.document_id} v${m.version}`, m.meeting_date, m.project_id, m.status, id].filter(Boolean).join(" · ") : id;
  item.append(el("div", { class: "source", "data-id": id, dir: "auto" },
    src ? src.text : "This chunk was not sent to the model — the citation is invalid.", el("span", { class: "src-meta", text: meta })));
}
/* ---------------- evidence: chunk popup ---------------- */
let chunkPop = null;   // { node, anchor } of the open popup
function closeChunk() {
  if (!chunkPop) return;
  chunkPop.anchor.classList.remove("open");
  chunkPop.node.remove();
  chunkPop = null;
}
function openChunk(anchor, r) {   // a small popup next to the Evidence item, with the chunk's text
  const same = chunkPop?.anchor === anchor;
  closeChunk();
  if (same) return;   // a second click on the same item closes it
  const meta = [r.source, `${r.document_id} v${r.version}`, r.meeting_date, r.project_id, r.status,
                `score ${r.score.toFixed(4)}`, r.chunk_id].filter(Boolean).join(" · ");
  const node = el("div", { class: "chunk-pop", role: "dialog", "aria-label": `Chunk #${r.rank}` },
    el("div", { class: "chunk-pop-head" },
      el("strong", { dir: "auto", text: `#${r.rank} ${r.title || r.document_id}` }),
      el("button", { type: "button", class: "chunk-close", title: "Close (Esc)", "aria-label": "Close", text: "✕", onclick: closeChunk })),
    el("div", { class: "chunk-pop-text", dir: "auto", text: r.text || "The chunk text was not saved with this run." }),
    el("span", { class: "src-meta", text: meta }));
  document.body.append(node);
  anchor.classList.add("open");
  chunkPop = { node, anchor };
  const box = anchor.getBoundingClientRect(), w = node.offsetWidth, h = node.offsetHeight, gap = 10;
  let left = box.left - w - gap, top = box.top;      // to the left of the item (Evidence is on the right)
  if (left < 8) { left = Math.min(Math.max(8, box.left), innerWidth - w - 8); top = box.bottom + gap; }   // no room: below it
  node.style.left = `${left}px`;
  node.style.top = `${Math.max(8, Math.min(top, innerHeight - h - 8))}px`;
  node.querySelector(".chunk-close").focus({ preventScroll: true });
}
document.addEventListener("click", (e) => {
  if (chunkPop && !chunkPop.node.contains(e.target) && !chunkPop.anchor.contains(e.target)) closeChunk();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && chunkPop) { const anchor = chunkPop.anchor; closeChunk(); anchor.focus(); }
});
document.addEventListener("scroll", (e) => { if (chunkPop && !chunkPop.node.contains(e.target)) closeChunk(); }, true);   // any panel scrolls
addEventListener("resize", closeChunk);

function resetTelemetry() {
  closeChunk();
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

/* ---------------- voice out: local MMS-TTS on the server, browser voice as fallback ---------------- */
let speechToken = 0;   // bumps on every speak/stop, so a slow synthesis that finishes late never plays
// Replies start by themselves (no Speak click). Browsers only let a page play sound after you have
// interacted with it, so every reply plays through ONE audio element that is unlocked by your first
// click or key press (the mic, Enter...). If a browser still holds the sound, it plays on your next
// click or key press anywhere on the page.
const player = new Audio();
player.preload = "auto";
let audioUnlocked = false;
function silence() {   // 50 ms of silent 8 kHz WAV, used once to unlock the player
  const samples = 400, bytes = new DataView(new ArrayBuffer(44 + samples));
  const text = (at, s) => [...s].forEach((c, i) => bytes.setUint8(at + i, c.charCodeAt(0)));
  text(0, "RIFF"); bytes.setUint32(4, 36 + samples, true); text(8, "WAVEfmt "); bytes.setUint32(16, 16, true);
  bytes.setUint16(20, 1, true); bytes.setUint16(22, 1, true); bytes.setUint32(24, 8000, true); bytes.setUint32(28, 8000, true);
  bytes.setUint16(32, 1, true); bytes.setUint16(34, 8, true); text(36, "data"); bytes.setUint32(40, samples, true);
  for (let i = 0; i < samples; i++) bytes.setUint8(44 + i, 128);
  return URL.createObjectURL(new Blob([bytes], { type: "audio/wav" }));
}
function unlockAudio() {
  if (audioUnlocked || state.audio) return;
  audioUnlocked = true;
  player.onended = player.onpause = player.onerror = null;
  player.src = silence();
  player.play().then(() => { if (!state.audio) player.pause(); })   // a reply may have started on the same click
    .catch(() => { audioUnlocked = false; });
}
for (const type of ["pointerdown", "keydown", "touchstart"]) document.addEventListener(type, unlockAudio, { capture: true, passive: true });
function speakWhenAllowed(text) {   // the browser held the sound: play it on the next click or key press
  stopSpeaking();
  const token = speechToken;
  toast("Your browser holds sound until you use the page — click anywhere or press a key and I'll speak.", "info");
  const go = () => {
    for (const type of ["pointerdown", "keydown"]) document.removeEventListener(type, go, true);
    if (token === speechToken) speak(text);   // nothing newer was said or stopped meanwhile
  };
  for (const type of ["pointerdown", "keydown"]) document.addEventListener(type, go, true);
}
function stopSpeaking() {
  speechToken++;
  window.speechSynthesis?.cancel();
  if (state.audio) {
    state.audio.pause();
    if (state.audio.src.startsWith("blob:")) URL.revokeObjectURL(state.audio.src);
    state.audio = null;
  }
  if (els.body.dataset.state === "speaking") setState("idle");
}
function browserVoice(lang) {
  return window.speechSynthesis?.getVoices().filter((v) => v.lang.toLowerCase().startsWith(lang)) || [];
}
const FIRST_CHUNK = 60;   // the first clip is cut short so playback starts quickly
const SPEECH_CHUNK = 160;   // characters per clip: about 7 s of CPU synthesis for 8 s of speech
function speechChunks(text) {   // sentences, split/merged to <= SPEECH_CHUNK; the first stays short so playback starts fast
  const out = [];
  for (let sentence of text.split(/(?<=[.!?;:\n])\s+/).map((s) => s.trim()).filter(Boolean)) {
    while (sentence.length > SPEECH_CHUNK) {
      let cut = Math.max(sentence.lastIndexOf(",", SPEECH_CHUNK), sentence.lastIndexOf(" ", SPEECH_CHUNK));
      if (cut <= 0) cut = SPEECH_CHUNK;
      out.push(sentence.slice(0, cut + 1).trim());
      sentence = sentence.slice(cut + 1).trim();
    }
    if (!sentence) continue;
    if (out.length > 1 && out[out.length - 1].length + sentence.length + 1 <= SPEECH_CHUNK) out[out.length - 1] += " " + sentence;
    else out.push(sentence);
  }
  const first = out[0];   // a short first clip: you hear the answer start after ~3 s instead of ~10 s
  if (first && first.length > FIRST_CHUNK) {
    let cut = Math.max(first.lastIndexOf(",", FIRST_CHUNK), first.lastIndexOf(" ", FIRST_CHUNK));
    if (cut > 15) out.splice(0, 1, first.slice(0, cut + 1).trim(), first.slice(cut + 1).trim());
  }
  return out;
}
async function synthesizeClip(text, lang) {   // one clip from the server's local MMS-TTS
  const response = await fetch("/api/speak", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, language: lang }),
  });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).error || "HTTP " + response.status);
  return { url: URL.createObjectURL(await response.blob()), info: JSON.parse(response.headers.get("X-TTS-Info") || "{}") };
}
/* The core "talks": while Jarvis speaks, the nucleus and a ring of voice bars follow the actual sound. */
const voiceViz = (() => {
  const BARS = 48, INNER = 52, MAX = 22;
  const group = $("voice"), bars = [];
  for (let i = 0; i < BARS; i++) {
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    group.append(line);
    bars.push({ line, cos: Math.cos((i / BARS) * 2 * Math.PI - Math.PI / 2), sin: Math.sin((i / BARS) * 2 * Math.PI - Math.PI / 2), v: 0 });
  }
  let context = null, analyser = null, freq = null, wave = null, running = false;
  function connect(audio) {   // route an <audio> through an analyser; if audio can't run yet, play it directly (fake motion)
    if (audio.dataset.viz) return;   // already routed: an element can be connected only once
    try {
      context ??= new AudioContext();
      if (context.state !== "running") context.resume().catch(() => {});
      if (context.state !== "running") return;   // never route into a suspended context: that would be silent
      if (!analyser) {
        analyser = context.createAnalyser();
        analyser.fftSize = 256;
        analyser.smoothingTimeConstant = 0.6;
        analyser.connect(context.destination);
        freq = new Uint8Array(analyser.frequencyBinCount);
        wave = new Uint8Array(analyser.fftSize);
      }
      context.createMediaElementSource(audio).connect(analyser);
      audio.dataset.viz = "1";
    } catch { /* visual only */ }
  }
  function frame(time) {
    if (els.body.dataset.state !== "speaking") {
      running = false;
      document.documentElement.style.setProperty("--level", "0");
      for (const b of bars) { b.v = 0; b.line.setAttribute("x2", 200 + b.cos * INNER); b.line.setAttribute("y2", 200 + b.sin * INNER); }
      return;
    }
    const real = analyser && state.audio?.dataset.viz && !state.audio.paused;
    let level;
    if (real) {
      analyser.getByteTimeDomainData(wave);
      analyser.getByteFrequencyData(freq);
      let sum = 0;
      for (const x of wave) sum += ((x - 128) / 128) ** 2;
      level = Math.min(1, Math.sqrt(sum / wave.length) * 4);
    } else {   // browser voice or no analyser: a syllable-like rhythm
      level = 0.35 + 0.3 * Math.abs(Math.sin(time / 110)) * (0.6 + 0.4 * Math.sin(time / 370));
    }
    document.documentElement.style.setProperty("--level", level.toFixed(3));
    bars.forEach((b, i) => {
      const k = Math.abs(i - BARS / 2) / (BARS / 2);   // mirrored: low frequencies at the top and bottom
      const target = real ? (freq[Math.floor((1 - k) * freq.length * 0.55)] / 255) * (0.4 + level)
        : level * (0.5 + 0.5 * Math.abs(Math.sin(time / 90 + i * 0.7)));
      b.v += (target - b.v) * 0.45;
      const r = INNER + 2 + b.v * MAX;
      b.line.setAttribute("x2", (200 + b.cos * r).toFixed(1));
      b.line.setAttribute("y2", (200 + b.sin * r).toFixed(1));
    });
    requestAnimationFrame(frame);
  }
  for (const b of bars) {
    b.line.setAttribute("x1", 200 + b.cos * INNER); b.line.setAttribute("y1", 200 + b.sin * INNER);
    b.line.setAttribute("x2", 200 + b.cos * INNER); b.line.setAttribute("y2", 200 + b.sin * INNER);
  }
  document.addEventListener("pointerdown", () => { try { (context ??= new AudioContext()).resume(); } catch {} }, { once: true });
  return { connect, start() { if (!running) { running = true; requestAnimationFrame(frame); } } };
})();
function playClip(url) {   // resolves when the clip ends or is stopped; rejects if the browser refuses to play
  return new Promise((resolve, reject) => {
    const audio = player;   // the one shared, unlocked element (see unlockAudio)
    voiceViz.connect(audio);   // once: the talking core follows the real sound
    audio.onended = audio.onpause = () => resolve();
    audio.onerror = () => reject(new Error("The browser could not play the audio"));
    audio.src = url;
    state.audio = audio;
    audio.play().catch(reject);
  });
}
async function speakStreamed(text, lang, token) {   // neural voice: one clip, played while the server streams it
  let prepared;
  try {
    const response = await fetch("/api/speak/prepare", {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text, language: lang }),
    });
    if (!response.ok) return false;
    prepared = await response.json();
  } catch { return false; }
  if (token !== speechToken) return true;   // stopped meanwhile
  if (prepared.engine !== "edge") return false;   // offline or JARVIS_TTS_ENGINE=local: sentence-by-sentence local voice
  setState("speaking", "Voice reply");
  const started = performance.now();
  try {
    const first = () => logLine(`🔊 neural voice · first audio in ${((performance.now() - started) / 1000).toFixed(2)}s`, "l-dim");
    player.addEventListener("playing", first, { once: true });
    try { await playClip(`/api/speak/${prepared.id}`); } finally { player.removeEventListener("playing", first); }
    if (token === speechToken) stopSpeaking();
    return true;
  } catch (error) {
    if (token !== speechToken) return true;
    if (error.name === "NotAllowedError") {   // autoplay policy: play on the next click / key press
      speakWhenAllowed(text);
      return true;
    }
    logLine("✕ neural voice failed: " + error.message, "l-err");
    return false;
  }
}
async function speak(text) {
  stopSpeaking();
  const token = speechToken;
  const lang = hasHebrew(text) ? "he" : "en";
  if (await speakStreamed(text, lang, token)) return;
  if (token !== speechToken) return;   // resolves when the speech ends or is stopped
  // Local fallback: Hebrew always goes to MMS-TTS (browsers rarely ship a Hebrew voice); English only when the browser has none.
  if (lang === "he" || !browserVoice("en").length) {
    const parts = speechChunks(text);
    if (!parts.length) return;
    setState("speaking", "Preparing voice (MMS-TTS, local)");
    let next = synthesizeClip(parts[0], lang);
    try {
      for (let i = 0; i < parts.length; i++) {
        const clip = await next;
        if (token !== speechToken) return URL.revokeObjectURL(clip.url);   // stopped while synthesizing
        next = i + 1 < parts.length ? synthesizeClip(parts[i + 1], lang) : null;   // prepare the next sentence while this one plays
        next?.catch(() => {});   // handled when awaited; avoids an "unhandled rejection" warning meanwhile
        logLine(`🔊 ${i + 1}/${parts.length} ${clip.info.model} · ${clip.info.audio_seconds}s audio in ${clip.info.seconds}s`, "l-dim");
        setState("speaking", `Sentence ${i + 1} of ${parts.length}`);
        try { await playClip(clip.url); } finally { URL.revokeObjectURL(clip.url); }
        if (token !== speechToken) return;
      }
      stopSpeaking();
      return;
    } catch (error) {
      if (token !== speechToken) return;
      if (error.name === "NotAllowedError") return speakWhenAllowed(text);   // autoplay policy: next click / key press
      stopSpeaking();
      logLine("✕ MMS-TTS failed: " + error.message, "l-err");
      if (lang === "he" && !browserVoice("he").length)
        return toast("Local Hebrew voice failed and the browser has no Hebrew voice: " + error.message);
    }
  }
  await speakInBrowser(text, lang);
}
function speakInBrowser(text, lang) {   // resolves when the utterance ends
  const synth = window.speechSynthesis;
  if (!synth) return setState("idle");
  if (!browserVoice(lang).length && lang === "he") { setState("idle"); return toast("This browser has no Hebrew voice."); }
  return new Promise((resolve) => {
  const utterance = new SpeechSynthesisUtterance(text.slice(0, 1200));
  const voices = browserVoice(lang);
  const preferred = lang === "en" ? voices.find((v) => /UK English Male|Daniel|George|Ryan/i.test(v.name)) : null;
  utterance.voice = preferred || voices[0] || null;
  utterance.lang = utterance.voice?.lang || (lang === "he" ? "he-IL" : "en-GB");
  utterance.rate = 1.02;
  utterance.onstart = () => setState("speaking");
  utterance.onend = utterance.onerror = (event) => {
    if (els.body.dataset.state === "speaking") setState("idle");
    if (event.error === "not-allowed") speakWhenAllowed(text);   // autoplay policy: next click / key press
    resolve();
  };
  synth.cancel();
  synth.speak(utterance);
  });
}

/* ---------------- voice in: record -> local Whisper ---------------- */
// End-of-speech detection: the threshold follows the room's noise floor, a word must last
// SPEECH_MS to count (a click or a cough doesn't), and silence ends the clip after SILENCE_SECONDS.
const SILENCE_SECONDS = 1.6, MAX_SECONDS = 60, NO_SPEECH_SECONDS = 8, SPEECH_MS = 150, MIN_THRESHOLD = 0.08;
// Two engines: the browser's own speech recognition (Chrome / Edge: instant, text appears while
// you speak, but the audio goes to the browser vendor's speech service), and local Whisper on
// the server (private, slower). The browser engine falls back to Whisper when it isn't usable.
const BrowserSpeech = window.SpeechRecognition || window.webkitSpeechRecognition;
const REVIEW_BELOW = 0.5;   // browser confidence under this: check the text instead of running it
function readyTone() {   // ~0.1 s "ding": the microphone is recording now
  try {
    const ctx = new AudioContext(), tone = ctx.createOscillator(), gain = ctx.createGain(), t = ctx.currentTime;
    tone.frequency.value = 880;
    gain.gain.setValueAtTime(0.0001, t);
    gain.gain.exponentialRampToValueAtTime(0.12, t + 0.01);
    gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.12);
    tone.connect(gain).connect(ctx.destination);
    tone.onended = () => ctx.close();
    tone.start(t);
    tone.stop(t + 0.13);
  } catch { /* the cue is optional */ }
}
const OPENING = "Opening microphone… wait for the tone";
// Warm microphone: opening the device takes ~0.5-1.5 s on every click, so after you use the mic it stays open
// for MIC_WARM_MINUTES (the browser shows the mic as in use), and the next question starts listening at once.
// It closes when the tab is hidden or after that idle time.
const MIC_WARM_MINUTES = 5;
const mic = { stream: null, timer: null };
async function openMic() {
  clearTimeout(mic.timer);
  if (mic.stream?.active) return mic.stream;
  mic.stream = await navigator.mediaDevices.getUserMedia({
    audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  return mic.stream;
}
function releaseMicLater() { clearTimeout(mic.timer); mic.timer = setTimeout(releaseMic, MIC_WARM_MINUTES * 60000); }
function releaseMic() { clearTimeout(mic.timer); mic.stream?.getTracks().forEach((t) => t.stop()); mic.stream = null; }
document.addEventListener("visibilitychange", () => { if (document.hidden && !state.recorder && !state.recognizer) releaseMic(); });
function micReady(clicked, engine) {   // how long from the click until the mic really records
  setState("listening", engine === "browser" ? "Speak now — text appears as you talk" : "Speak now — I stop when you pause");
  readyTone();
  logLine(`🎙 ${engine} mic ready in ${((performance.now() - clicked) / 1000).toFixed(2)}s`, "l-dim");
}
function toggleRecording() {
  if (state.recognizer) return state.recognizer.stop();
  if (state.recorder) return state.recorder.stop();
  if (els.engine.value === "browser" && BrowserSpeech && !state.browserSpeechBroken) return listenInBrowser();
  return recordWhisper();
}
function listenInBrowser() {
  if (!state.ended) return toast("Wait for the current run to finish.", "info");
  stopSpeaking();
  const recognizer = new BrowserSpeech();
  // The browser can't detect the language itself; Auto listens in Hebrew, which still catches English names.
  recognizer.lang = els.lang.value === "en" ? "en-US" : "he-IL";
  recognizer.interimResults = true;   // live text while speaking
  recognizer.continuous = false;      // one question; the browser ends it when you pause
  recognizer.maxAlternatives = 1;
  const started = performance.now();
  let finalText = "", interimText = "", confidence = null, failure = null;
  const level = (value) => document.documentElement.style.setProperty("--level", value);
  els.mic.classList.add("recording");
  setState("listening", OPENING);   // words said before the tone are lost
  if (navigator.mediaDevices?.getUserMedia) openMic().catch(() => {});   // keeps the device warm for the next question
  recognizer.onaudiostart = () => micReady(started, "browser");
  recognizer.onspeechstart = () => level("0.6");
  recognizer.onspeechend = () => level("0");
  recognizer.onresult = (event) => {
    interimText = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const result = event.results[i];
      if (result.isFinal) { finalText += result[0].transcript; confidence = result[0].confidence; }
      else interimText += result[0].transcript;
    }
    els.question.value = (finalText + interimText).trim();
    autosize();
  };
  recognizer.onerror = (event) => { failure = event.error; };
  recognizer.onend = () => {
    state.recognizer = null;
    releaseMicLater();
    els.mic.classList.remove("recording");
    level("0");
    setState("idle");
    if (failure === "not-allowed") return toast("Microphone permission was denied.");
    if (failure && !["no-speech", "aborted"].includes(failure)) {
      // network, service-not-allowed, language-not-supported, audio-capture: use the local engine instead
      state.browserSpeechBroken = true;
      toast(`Browser speech recognition is unavailable (${failure}) — using local Whisper.`, "info");
      return recordWhisper();
    }
    const text = (finalText || interimText).trim();
    if (!text) return toast("I didn't hear anything.", "info");
    els.question.value = text;
    autosize();
    const sure = confidence === null || confidence === 0 ? "" : ` · confidence ${Math.round(confidence * 100)}%`;
    logLine(`🎙 browser speech · ${recognizer.lang}${sure} · ${((performance.now() - started) / 1000).toFixed(1)}s`, "l-dim");
    if (confidence && confidence < REVIEW_BELOW) {
      els.question.focus();
      return toast("I'm not sure I heard that right — check the text, then press Enter.", "info");
    }
    submit();   // speak -> run, like a voice assistant
  };
  state.recognizer = recognizer;
  try { recognizer.start(); }
  catch { state.recognizer = null; state.browserSpeechBroken = true; return recordWhisper(); }
}
async function recordWhisper() {
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) return toast("This browser cannot record audio.");
  if (!state.ended) return toast("Wait for the current run to finish.", "info");
  let stream;
  const clicked = performance.now();
  els.mic.classList.add("recording");
  setState("listening", OPENING);
  try { stream = await openMic(); }
  catch { els.mic.classList.remove("recording"); setState("idle"); return toast("Microphone permission was denied."); }
  stopSpeaking();
  const type = ["audio/webm;codecs=opus", "audio/ogg;codecs=opus", "audio/webm"].find((t) => MediaRecorder.isTypeSupported(t)) || "";
  const recorder = new MediaRecorder(stream, type ? { mimeType: type } : undefined);
  const chunks = [];
  const audio = new AudioContext();
  const analyser = audio.createAnalyser();
  analyser.fftSize = 1024;
  audio.createMediaStreamSource(stream).connect(analyser);
  const samples = new Uint8Array(analyser.fftSize);
  const started = performance.now();
  let heard = false, quietSince = null, loudSince = null, floor = null, frame;
  const meter = () => {   // drives the core with the voice level and stops after a pause
    analyser.getByteTimeDomainData(samples);
    let sum = 0;
    for (const s of samples) sum += ((s - 128) / 128) ** 2;
    const level = Math.min(1, Math.sqrt(sum / samples.length) * 6);
    document.documentElement.style.setProperty("--level", level.toFixed(3));
    const now = performance.now();
    floor ??= Math.min(level, 0.02);   // never learn the floor from the first frame: you may already be talking
    const threshold = Math.max(MIN_THRESHOLD, floor * 2.5 + 0.03);
    if (level > threshold) {
      loudSince ??= now;
      if (now - loudSince >= SPEECH_MS) { heard = true; quietSince = null; }
    } else {
      loudSince = null;
      floor = level < floor ? floor * 0.7 + level * 0.3 : floor * 0.98 + level * 0.02;   // quiet frames only: falls fast, rises slowly
      if (heard) quietSince ??= now;
    }
    const silent = heard && quietSince && now - quietSince > SILENCE_SECONDS * 1000;
    const nothing = !heard && now - started > NO_SPEECH_SECONDS * 1000;
    if (silent || nothing || now - started > MAX_SECONDS * 1000) return recorder.stop();
    frame = requestAnimationFrame(meter);
  };
  recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
  recorder.onstop = async () => {
    cancelAnimationFrame(frame);
    releaseMicLater();   // the stream stays open for the next question
    audio.close();
    document.documentElement.style.setProperty("--level", "0");
    state.recorder = null;
    els.mic.classList.remove("recording");
    if (!heard) { setState("idle"); return toast("I didn't hear anything.", "info"); }
    await transcribe(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
  };
  state.recorder = recorder;
  recorder.start();
  micReady(clicked, "whisper");
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
    logLine(`🎙 whisper ${result.model} · ${result.language} · confidence ${Math.round((result.confidence ?? 1) * 100)}% · `
            + `${result.audio_seconds}s audio in ${result.seconds}s`, "l-dim");
    setState("idle");
    if (result.review) {   // doubtful transcript: let the user fix it instead of running the wrong question
      els.question.focus();
      return toast("I'm not sure I heard that right — check the text, then press Enter.", "info");
    }
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
  els.speak.addEventListener("change", () => { store.set("speak", els.speak.checked); if (!els.speak.checked) stopSpeaking(); });
  els.lang.addEventListener("change", () => store.set("lang", els.lang.value));
  if (!BrowserSpeech) els.engine.querySelector('[value="browser"]').disabled = true;   // e.g. Firefox
  els.engine.value = BrowserSpeech ? store.get("engine", "browser") : "whisper";
  els.engine.addEventListener("change", () => { store.set("engine", els.engine.value); state.browserSpeechBroken = false; });
  $("composer").addEventListener("submit", submit);
  els.question.addEventListener("input", autosize);
  els.question.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) submit(e); });
  els.mic.addEventListener("click", toggleRecording);
  els.cancel.addEventListener("click", cancelRun);
  $("refresh-status").addEventListener("click", () => loadStatus(true));
  document.addEventListener("keydown", (e) => { if (e.ctrlKey && e.code === "Space") { e.preventDefault(); toggleRecording(); } });
  if (!window.MediaRecorder && !BrowserSpeech) { els.mic.disabled = true; els.mic.title = "Recording is not supported in this browser"; }
  window.speechSynthesis?.getVoices();   // some browsers load voices lazily
  loadSkills().catch((error) => toast(error.message));
  if (FEEDBACK) loadFeedbackIssues().catch(() => {});   // labels for the report form and the history; retried when the form opens
  const linked = new URLSearchParams(location.hash.slice(1)).get("run");   // deep link: /#run=<id>
  loadHistory().then((data) => { if (data?.active) follow(data.active); else if (linked) openRun(linked); });
  boot();
  setInterval(() => { if (state.ended) loadStatus(); }, 30000);
  setInterval(() => { if (state.ended) loadHistory(); }, 5000);   // shows vault syncs started by the watcher
}
init();
