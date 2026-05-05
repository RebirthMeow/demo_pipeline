// review UI client
//
// State lives entirely on the server (review_state.json + the four
// aggregated JSONs).  The client just fetches /api/clips when it needs to
// resync, posts to /api/label on votes, and re-fetches after each vote so
// the strip + counts stay correct.

const $  = sel => document.querySelector(sel);
const $$ = sel => document.querySelectorAll(sel);

const els = {
  vid:    $("#vid"),
  meta:   $("#meta"),
  counts: $("#counts"),
  strip:  $("#strip"),
  finish: $("#finish-btn"),
  modal:  $("#modal"),
  modalContent: $("#modal-content"),
};

let state = {
  clips:   [],     // from /api/clips
  counts:  {},
  index:   0,      // currently-shown clip index
  muted:   true,   // persisted across clip switches; flipped by 'm' or the mute button
};


// ── api ────────────────────────────────────────────────────────────────
async function fetchClips() {
  const r = await fetch("/api/clips");
  if (!r.ok) throw new Error("failed to fetch /api/clips");
  const data = await r.json();
  state.clips  = data.clips;
  state.counts = data.counts;
  return data;
}

async function postLabel(clip_id, label) {
  const r = await fetch("/api/label", {
    method:  "POST",
    headers: {"Content-Type": "application/json"},
    body:    JSON.stringify({clip_id, label}),
  });
  return r.json();
}


// ── render ──────────────────────────────────────────────────────────────
function renderCounts() {
  const c = state.counts || {};
  const total = state.clips.length;
  const reviewed = total - (c.unset || 0);
  els.counts.innerHTML = `
    ${reviewed}/${total} reviewed
    <span class="pip good">good ${c.good || 0}</span>
    <span class="pip ok">ok ${c.ok || 0}</span>
    <span class="pip bad">bad ${c.bad || 0}</span>
    <span class="pip ignore">ignore ${c.ignore || 0}</span>
    <span class="pip unset">unset ${c.unset || 0}</span>
  `;
  // Finish is enabled as soon as anything's labeled.  An all-unset session
  // is meaningless to log — the modal would have no signal to show.
  if (els.finish) els.finish.disabled = reviewed === 0;
}

function renderStrip() {
  els.strip.innerHTML = "";
  state.clips.forEach((c, i) => {
    const d = document.createElement("div");
    d.className = "pip" +
      (c.has_video ? " has-video" : "") +
      (c.label ? ` l-${c.label}` : "") +
      (i === state.index ? " current" : "");
    d.textContent = c.clip_id;
    d.title = `${c.weapon}  ${c.attacker_name} → ${c.target_name}  score=${(c.score || 0).toFixed(3)}` +
              (c.label ? `\nlabel: ${c.label}` : "");
    d.addEventListener("click", () => { state.index = i; renderCurrent(); });
    els.strip.appendChild(d);
  });
}

function renderCurrent() {
  const c = state.clips[state.index];
  if (!c) {
    els.vid.removeAttribute("src");
    els.meta.textContent = "no clips";
    return;
  }

  // Set video src — if not has_video we still try, server returns 404
  if (c.has_video) {
    const url = `/video/${c.clip_id}.mp4`;
    if (els.vid.src.indexOf(url) === -1) {
      els.vid.src = url;
      els.vid.muted = state.muted;
      els.vid.loop  = true;
      els.vid.load();
      els.vid.play().catch(() => { /* autoplay can be blocked, fine */ });
    }
  } else {
    els.vid.removeAttribute("src");
  }
  syncPlayerButtons();

  els.meta.innerHTML = `
    <span class="k">clip</span>      <span class="v">${c.clip_id}  (${state.index + 1}/${state.clips.length})</span>
    <span class="k">weapon</span>    <span class="v weapon">${c.weapon}</span>
    <span class="k">match</span>     <span class="v">${c.human_time}  raw=${c.time_raw}</span>
    <span class="k">attacker</span>  <span class="v who"><span class="qname">${escape(c.attacker_name)}</span></span>
    <span class="k">target</span>    <span class="v who"><span class="qname">${escape(c.target_name)}</span></span>
    <span class="k">score</span>     <span class="v score">${(c.score || 0).toFixed(4)}</span>
    <span class="k">source</span>    <span class="v">${escape(c.source_demo)}</span>
    <span class="k">label</span>     <span class="v">${c.label || "(unset)"}${c.label_ts ? "  @ " + c.label_ts : ""}</span>
  `;

  // active vote button
  $$("button.vote").forEach(b => b.classList.toggle("active", b.dataset.label === c.label));

  renderStrip();
}

function escape(s) {
  return String(s).replace(/[&<>"']/g, ch => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[ch]);
}


// ── actions ─────────────────────────────────────────────────────────────
async function doVote(label) {
  const c = state.clips[state.index];
  if (!c) return;
  // Toggle off if clicking the active label
  const next = (c.label === label) ? "unset" : label;
  toast(next === "unset" ? `unset ${c.clip_id}` : `${c.clip_id}: ${next}`, "ok");
  const r = await postLabel(c.clip_id, next);
  if (!r.ok) {
    toast(`error: ${r.error || "unknown"}`, "err");
    return;
  }
  await fetchClips();
  // auto-advance on a new vote (not on toggle-off and not on the last clip)
  if (next !== "unset" && state.index < state.clips.length - 1) {
    state.index += 1;
  }
  renderCurrent();
  renderCounts();
}

async function doUndo() {
  const c = state.clips[state.index];
  if (!c || !c.label) { toast("nothing to undo", "err"); return; }
  const r = await postLabel(c.clip_id, "unset");
  if (!r.ok) { toast(`error: ${r.error || "unknown"}`, "err"); return; }
  await fetchClips();
  renderCurrent();
  renderCounts();
  toast(`undone ${c.clip_id}`, "ok");
}

function nav(d) {
  const next = state.index + d;
  if (next < 0 || next >= state.clips.length) return;
  state.index = next;
  renderCurrent();
}

// ── player controls ─────────────────────────────────────────────────────
function togglePlay() {
  if (els.vid.paused) {
    els.vid.play().catch(() => {});
  } else {
    els.vid.pause();
  }
  syncPlayerButtons();
}

function toggleMute() {
  state.muted = !state.muted;
  els.vid.muted = state.muted;
  syncPlayerButtons();
}

function replay() {
  els.vid.currentTime = 0;
  els.vid.play().catch(() => {});
  syncPlayerButtons();
}

function seekBy(delta) {
  // delta in seconds. clamp to [0, duration].
  const d = els.vid.duration;
  if (!isFinite(d) || d <= 0) return;
  els.vid.currentTime = Math.max(0, Math.min(d - 0.001, els.vid.currentTime + delta));
}

function frameStep(direction) {
  // 60fps clips → ~16.67ms per frame. Pause first, then nudge.
  els.vid.pause();
  seekBy(direction * (1 / 60));
  syncPlayerButtons();
}

function syncPlayerButtons() {
  const playBtn = $(".nav.play");
  if (playBtn) playBtn.textContent = els.vid.paused ? "▶ play" : "⏸ pause";
  const muteBtn = $(".nav.mute");
  if (muteBtn) muteBtn.textContent = els.vid.muted ? "🔇 unmute" : "🔊 mute";
}


// ── toast ───────────────────────────────────────────────────────────────
let toastTimer = null;
function toast(msg, kind = "ok") {
  let t = $(".toast");
  if (!t) {
    t = document.createElement("div");
    t.className = "toast";
    document.body.appendChild(t);
  }
  t.className = `toast show ${kind}`;
  t.textContent = msg;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 1400);
}


// ── close-out modal ─────────────────────────────────────────────────────
//
// Three-step flow:
//   1. confirm   — user clicks Finish, we show a preview of what'll be logged
//   2. saved     — they confirm, server writes session log, we show "saved"
//   3. closed    — they shut down, server os._exit(0)s, we show "you can close"
//
// Each render replaces #modal-content; Cancel/keep-running just hides #modal.

function openModal()  { els.modal.classList.remove("hidden"); els.modal.setAttribute("aria-hidden", "false"); }
function closeModal() { els.modal.classList.add("hidden");    els.modal.setAttribute("aria-hidden", "true"); }

function renderModalConfirm() {
  const c = state.counts || {};
  const total    = state.clips.length;
  const reviewed = total - (c.unset || 0);
  const unset    = c.unset || 0;

  // per-clip mini-table — order matches the strip
  const rows = state.clips.map(cl => `
    <tr>
      <td>${cl.clip_id}</td>
      <td><span class="pill ${cl.label || "unset"}">${cl.label || "unset"}</span></td>
      <td>${escape((cl.weapon || "?").replace("MOD_", ""))}</td>
      <td>${(cl.score || 0).toFixed(3)}</td>
    </tr>
  `).join("");

  const warnIfUnset = unset > 0
    ? `<div class="warn">${unset} clip(s) still unlabeled — they'll be recorded as "unset" in the session log.  You can close this modal, finish reviewing, then come back to lock in.</div>`
    : "";

  els.modalContent.innerHTML = `
    <h2>Finish session?</h2>
    <div class="summary-grid">
      <span class="k">total clips</span>      <span class="v">${total}</span>
      <span class="k">reviewed</span>         <span class="v">${reviewed}</span>
      <span class="k">good / ok / bad</span>  <span class="v">${c.good || 0} / ${c.ok || 0} / ${c.bad || 0}</span>
      <span class="k">ignore</span>           <span class="v">${c.ignore || 0}</span>
      <span class="k">unset</span>            <span class="v">${unset}</span>
    </div>
    ${warnIfUnset}
    <table class="clip-table">
      <thead><tr><th>clip</th><th>label</th><th>weapon</th><th>score</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p style="color: var(--dim); font-size: 12px; margin: 8px 0 0;">
      Saving writes a snapshot to <code>review/sessions/session_*.json</code>.
      Labels are already durable in the aggregated training files — this just
      records what happened in this session for the audit trail.
    </p>
    <div class="modal-actions">
      <button class="btn-secondary" id="m-cancel">Cancel</button>
      <button class="btn-primary"   id="m-confirm">Save &amp; lock in</button>
    </div>
  `;
  $("#m-cancel").addEventListener("click", closeModal);
  $("#m-confirm").addEventListener("click", doFinish);
}

// stash the next-step command from the server so renderModalClosed can show it
let _lastNextCmd = "";

function renderModalSaved(summary) {
  const cd = summary.corpus_delta || {};
  const dur = summary.started_at && summary.finished_at
    ? `${summary.started_at} → ${summary.finished_at}`
    : "—";

  // remember what the server suggested for the post-shutdown screen
  _lastNextCmd = summary.next_cmd || "";

  els.modalContent.innerHTML = `
    <h2><span class="ok-mark">✓</span>session saved</h2>
    <div class="summary-grid">
      <span class="k">duration</span>     <span class="v">${escape(dur)}</span>
      <span class="k">log file</span>     <span class="v"><code>${escape(summary.log_path || "?")}</code></span>
      <span class="k">good +</span>       <span class="v">${cd.good   || 0}</span>
      <span class="k">ok +</span>         <span class="v">${cd.ok     || 0}</span>
      <span class="k">bad +</span>        <span class="v">${cd.bad    || 0}</span>
      <span class="k">ignore +</span>     <span class="v">${cd.ignore || 0}</span>
    </div>
    <p style="color: var(--dim); font-size: 12px; margin: 8px 0 0;">
      The session log is durable.  Server's still running — you can close this
      and keep labelling (a new session opens automatically), or shut down now.
    </p>
    <div class="modal-actions">
      <button class="btn-secondary" id="m-keep">Keep server running</button>
      <button class="btn-danger"    id="m-shutdown">Shutdown server</button>
    </div>
  `;
  $("#m-keep").addEventListener("click", closeModal);
  $("#m-shutdown").addEventListener("click", doShutdown);
}

function renderModalClosed() {
  // The server hands us next_cmd in the /api/finish response.  If somehow we
  // didn't capture it (user shut down without clicking Finish first), fall
  // back to a generic placeholder.
  const nextCmd = _lastNextCmd
    || "cd <repo>/python/predict\n.\\venv\\Scripts\\python.exe predict_frags_ensemble.py _jactf_new_frags.json";
  els.modalContent.innerHTML = `
    <h2><span class="ok-mark">✓</span>session closed</h2>
    <p>Server has been shut down.  You can close this tab.</p>
    <p style="color: var(--dim); font-size: 13px; margin: 14px 0 6px;">
      Suggested next step (re-train predict against the updated corpus):
    </p>
    <div class="next-step">${escape(nextCmd)}</div>
  `;
}

async function doFinish() {
  try {
    const r = await fetch("/api/finish", {method: "POST"});
    const data = await r.json();
    if (!data.ok) {
      toast(`finish error: ${data.error || "unknown"}`, "err");
      return;
    }
    renderModalSaved(data);
  } catch (e) {
    toast(`finish error: ${e.message}`, "err");
  }
}

async function doShutdown() {
  try {
    const r = await fetch("/api/shutdown", {method: "POST"});
    // We don't actually need the response body — the server is exiting.
    // Render the final state regardless.
    renderModalClosed();
  } catch (e) {
    // network error is expected if the server died before responding.
    renderModalClosed();
  }
}

function startFinishFlow() {
  if (els.finish.disabled) return;
  renderModalConfirm();
  openModal();
}


// ── wiring ──────────────────────────────────────────────────────────────
$$("button.vote").forEach(b => b.addEventListener("click", () => doVote(b.dataset.label)));
$(".nav.prev").addEventListener("click", () => nav(-1));
$(".nav.next").addEventListener("click", () => nav(+1));
$(".nav.undo").addEventListener("click", doUndo);
$(".nav.play").addEventListener("click", togglePlay);
$(".nav.replay").addEventListener("click", replay);
$(".nav.mute").addEventListener("click", toggleMute);
els.finish.addEventListener("click", startFinishFlow);

// keep the pause/mute buttons honest if the user uses the native <video>
// controls or remote keyboard media keys — no ground-truth in JS otherwise.
els.vid.addEventListener("play",         syncPlayerButtons);
els.vid.addEventListener("pause",        syncPlayerButtons);
els.vid.addEventListener("volumechange", syncPlayerButtons);

document.addEventListener("keydown", e => {
  // ignore when typing in form fields
  if (e.target.matches("input, textarea")) return;
  // ignore when focus is inside the native <video> controls — let the
  // browser handle space/arrows so its built-in scrubbing isn't fought.
  // (Native <video> only steals focus when clicked into; for keyboard
  // shortcuts on the page chrome we want our bindings to win.)
  if (e.target === els.vid) return;
  // don't interfere with browser shortcuts
  if (e.ctrlKey || e.metaKey || e.altKey) return;

  switch (e.key) {
    case "1": doVote("bad");    e.preventDefault(); break;
    case "2": doVote("ok");     e.preventDefault(); break;
    case "3": doVote("good");   e.preventDefault(); break;
    case "4": doVote("ignore"); e.preventDefault(); break;

    case "ArrowLeft":  nav(-1); e.preventDefault(); break;
    case "ArrowRight": nav(+1); e.preventDefault(); break;

    case " ":          togglePlay(); e.preventDefault(); break;

    case "j": case "J": seekBy(-1); e.preventDefault(); break;
    case "l": case "L": seekBy(+1); e.preventDefault(); break;
    case ",": frameStep(-1); e.preventDefault(); break;
    case ".": frameStep(+1); e.preventDefault(); break;

    case "u": case "U": doUndo();    e.preventDefault(); break;
    case "m": case "M": toggleMute(); e.preventDefault(); break;
    case "r": case "R": replay();     e.preventDefault(); break;

    case "Escape":
      // close the modal if it's open; do nothing otherwise (don't fight
      // any future native UI handlers)
      if (!els.modal.classList.contains("hidden")) {
        closeModal();
        e.preventDefault();
      }
      break;
  }
});


// ── boot ────────────────────────────────────────────────────────────────
(async function init() {
  try {
    await fetchClips();
    // start at first unlabeled clip if any, else 0
    const firstUnset = state.clips.findIndex(c => !c.label);
    state.index = firstUnset >= 0 ? firstUnset : 0;
    renderCurrent();
    renderCounts();
  } catch (e) {
    els.meta.textContent = `init error: ${e.message}`;
  }
})();
