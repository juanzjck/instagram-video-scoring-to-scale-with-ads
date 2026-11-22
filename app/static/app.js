// Review card UI. Everything from the video (captions, on-screen text, comments, model
// output) is untrusted, so it only ever reaches the DOM through esc().

const $ = (s, el = document) => el.querySelector(s);
const main = $("#main");
const state = { view: "home", videoId: null, card: null, startedAt: null, timerId: null,
  job: null, pollId: null };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mode = () => $('input[name="mode"]:checked').value;
const fmtTs = (t) => (t == null ? "n/a" : `${Math.floor(t / 60)}:${String(Math.floor(t % 60)).padStart(2, "0")}`);
const pct = (x, dp = 2) => (x == null ? null : `${(x * 100).toFixed(dp)}%`);
const int = (x) => (x == null ? "not available" : Math.round(x).toLocaleString("en-US"));
const label = (v) => (v ? v.replaceAll("_", " ") : "—");
const chip = (v) => `<span class="chip ${v ? esc(v) : "muted"}">${esc(v ? label(v) : "not scored")}</span>`;

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) { /* not json */ }
    const err = new Error(typeof msg === "string" ? msg : msg.message || JSON.stringify(msg));
    err.detail = msg;
    err.status = r.status;
    throw err;
  }
  return r.headers.get("content-type")?.includes("json") ? r.json() : r.text();
}

// m:ss in already-escaped text -> a button that seeks the player
const linkTs = (escaped) => escaped.replace(/\b(\d{1,2}):(\d{2})\b/g,
  (m, a, b) => `<button class="ts" data-t="${+a * 60 + +b}" title="Jump to ${m}">${m}</button>`);

function seek(t) {
  const v = $("video");
  if (!v) return;
  v.currentTime = t;
  v.play().catch(() => {});
}

// ------------------------------------------------------------------ sidebar
// Views (video list, card, Evaluation) follow the open session's own run prompt: the server defaults to it, so no
// ?prompt= is sent. The Dataset tab's prompt selector (state.runPrompt) only chooses the prompt of the NEXT run.
const videosUrl = () => "/api/videos";
const listVideos = () => api(videosUrl());
async function loadList() {
  const vids = await listVideos();
  $("#process-all").disabled = !vids.length || !!state.job?.running;
  if (!vids.length) {
    $("#video-list").innerHTML = '<li class="hint">No videos yet. Fill in the form in the Dataset tab.</li>';
    return;
  }
  $("#video-list").innerHTML = vids.map((v) => `
    <li><button data-id="${esc(v.video_id)}" class="${v.video_id === state.videoId ? "active" : ""}">
      <span class="row1"><span>${esc(v.video_id)}${v.decided ? " ✓" : ""}</span>${chip(v.verdict)}</span>
      ${v.prompt_id !== v.active_prompt ? `<span class="cap">prompt ${esc(v.prompt_id)} (active: ${esc(v.active_prompt)})</span>` : ""}
      <span class="cap">${esc(v.brand)} · @${esc(v.creator)}</span>
      <span class="cap">${esc(v.caption)}</span>
    </button></li>`).join("");
}

// ------------------------------------------------------------------ dataset + pipeline run
const STEPS = { queued: "queued", downloading: "downloading video…", preprocessing: "keyframes + transcript…",
  analysing: "visual analysis…", scoring: "scoring…" };

// What one video is doing: the running job's word for it, else where it stands on disk.
// Seconds as "48s" or "3m 12s".
const dur = (s) => (s == null ? "—" : s < 60 ? `${s < 10 ? s.toFixed(1) : Math.round(s)}s` : `${Math.floor(s / 60)}m ${String(Math.round(s % 60)).padStart(2, "0")}s`);

function videoStatus(v, job) {
  const j = job?.videos?.[v.video_id];
  if (j?.state === "error") return `<span class="err">${esc(j.error)}</span>`;
  if (j && j.state !== "done") return `<span class="busy">${esc(STEPS[j.state] || j.state)}</span>`;
  if (v.scored) return `${chip(v.verdict)}${v.prompt_id !== v.active_prompt ? ` <span class="hint">${esc(v.prompt_id)}</span>` : ""}${v.seconds != null ? ` <span class="hint" title="End-to-end pipeline time of its last full run">${dur(v.seconds)}</span>` : ""}`;
  if (v.source_url) return v.has_clip ? "downloaded, not scored" : "URL set, not downloaded";
  return v.has_clip ? "clip in dataset, not scored" : '<span class="na">no video: add a URL or file</span>';
}

function jobLine(job) {
  if (!job?.started_at) return "";
  const errors = Object.values(job.videos).filter((v) => v.state === "error").length;
  const elapsed = job.running ? (Date.now() - new Date(job.started_at)) / 1000 : job.elapsed_s;
  return job.running ? `Running: ${job.done} / ${job.total} videos done · ${dur(elapsed)} so far`
    : `Finished: ${job.total - errors} of ${job.total} videos processed${errors ? `, ${errors} failed` : ""}${elapsed != null ? ` in ${dur(elapsed)}` : ""}
      (videos already scored reuse their cached steps, so they finish fast).`;
}

// The dataset form. One editable table per data-template file (answer keys excluded);
// the columns come from /api/dataset/schema so the server and the form never disagree.
const REF_KEY = { "campaigns.csv": "campaign_id", "creators.csv": "creator_id", "videos.csv": "video_id" };
const CAMPAIGN = "campaigns.csv";          // one session = one campaign: a single record, asked first
const campaign = () => form.data[CAMPAIGN][0];
const campaignReady = () => !!(String(campaign().campaign_id ?? "").trim() && String(campaign().brand ?? "").trim());
const GENERATED = ["media.csv", "keyframes.csv", "transcripts.csv"];   // pipeline output, not form input
const form = { schema: null, data: null, errors: [] };
state.openSessions = new Set();
state.openCollections = new Set();
state.collections = [];
document.addEventListener("toggle", (ev) => {
  const d = ev.target;
  if (d.matches?.("details.sess")) state.openSessions[d.open ? "add" : "delete"](d.dataset.session);
  else if (d.matches?.("details.coll")) state.openCollections[d.open ? "add" : "delete"](d.dataset.collection);
}, true);

const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const formCols = (file) => form.schema.files[file].cols.filter((c) => !c.auto);
const blankRow = (file) => Object.fromEntries(formCols(file).map((c) => [c.name, ""]));

// The form is remembered server-side in the open session's session.json, saved as you type.
async function loadForm() {
  if (form.schema) return;
  form.schema = await api("/api/dataset/schema");
  setForm((await api("/api/session"))?.form);
}

function setForm(tables) {
  form.errors = [];
  if (!tables || !Object.values(tables).some((rows) => rows?.length)) { clearForm(); return; }
  form.data = {};
  for (const file of Object.keys(form.schema.files)) form.data[file] = (tables[file] || []).map((r) => ({ ...blankRow(file), ...r }));
  form.data[CAMPAIGN] = form.data[CAMPAIGN].slice(0, 1);
  if (!form.data[CAMPAIGN].length) form.data[CAMPAIGN].push(blankRow(CAMPAIGN));
}

function clearForm() {
  form.data = Object.fromEntries(Object.keys(form.schema.files).map((f) =>
    [f, form.schema.required.includes(f) ? [blankRow(f)] : []]));
  form.errors = [];
}

const formIsBlank = () => !Object.values(form.data).some((rows) =>
  rows.some((r) => Object.values(r).some((v) => String(v ?? "").trim())));

const localTime = (iso) => (iso ? new Date(iso).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "");

function setSaveStatus(text) { const el = $("#session-status"); if (el) el.textContent = text; }

// Debounced autosave. The first save of a fresh app opens a new session, so an untouched
// blank form never creates one. Saves run one after another, so two can't both create a session.
function saveDraft() {
  if (!state.session && formIsBlank()) return;
  clearTimeout(state.saveId);
  state.dirty = true;
  setSaveStatus("Unsaved changes…");
  state.saveId = setTimeout(flushDraft, 700);
}

function draftBody() {
  return JSON.stringify({ tables: form.data });     // the server names the session after its campaign
}

function flushDraft() {
  clearTimeout(state.saveId);
  if (!state.dirty) return state.saving || Promise.resolve();
  state.dirty = false;
  state.saving = (state.saving || Promise.resolve()).then(async () => {
    try {
      const s = await api("/api/session", { method: "PUT", headers: { "content-type": "application/json" }, body: draftBody() });
      const opened = s.id !== state.session?.id;
      state.session = s;
      setSaveStatus(`Saved ${new Date().toLocaleTimeString()} · uploads/${s.id}/session.json`);
      if ($("#session-title")) $("#session-title").textContent = s.name;
      refreshCrumb();
      if (opened) { $("#ds-reset")?.removeAttribute("disabled"); refreshSessionList(); }
    } catch (e) { state.dirty = true; setSaveStatus(`Not saved: ${e.message}`); }
  });
  return state.saving;
}

// Closing the tab with a save pending: hand it to the browser to send.
window.addEventListener("pagehide", () => {
  if (state.dirty && form.data) navigator.sendBeacon("/api/session", new Blob([draftBody()], { type: "application/json" }));
});

async function refreshSessionList() {
  const el = $("#session-list");
  if (!el) return;
  const [list, colls] = await Promise.all([api("/api/sessions").catch(() => []), api("/api/collections").catch(() => [])]);
  state.collections = colls;
  el.innerHTML = sessionList(list);
}

async function copySession(btn) {
  const id = btn.dataset.copySession, msg = main.querySelector(`[data-copy-msg="${CSS.escape(id)}"]`);
  btn.disabled = true;
  if (msg) msg.textContent = "Copying… (clips included, can take a moment)";
  try {
    await flushDraft();                      // copy what was typed, not an older save
    const copy = await api(`/api/sessions/${encodeURIComponent(id)}/copy`, { method: "POST" });
    state.openSessions.add(copy.id);
    await refreshSessionList();
    const el = main.querySelector(`[data-copy-msg="${CSS.escape(copy.id)}"]`);
    if (el) el.textContent = `Copied as "${copy.name}".`;
  } catch (e) { btn.disabled = false; if (msg) msg.textContent = `Not copied: ${e.message}`; }
}

// Deleting a session is for good, so it goes through a modal that lists what is lost and asks
// for the campaign's name to be typed.
async function askDeleteSession(id) {
  const list = await api("/api/sessions").catch(() => []);
  const x = list.find((s) => s.id === id);
  if (!x) return refreshSessionList();
  const r = x.rows || {}, reviews = Object.entries(x.manual_review || {});
  const lost = [
    `${plural(r["creators.csv"] || 0, "creator")}, ${plural(r["videos.csv"] || 0, "video")}, ${plural(r["comments.csv"] || 0, "comment")} in the form`,
    x.generated && "the generated dataset CSVs",
    x.scored && `pipeline results for ${plural(x.scored, "video")}, and their clips`,
    x.recaps && plural(x.recaps, "recap"),
    x.decisions && plural(x.decisions, "review decision"),
    reviews.length && `manual reviews by ${reviews.map(([n, s]) => `${n}${s.submitted_at ? " (submitted)" : ""}`).join(", ")}`,
    x.ground_truth?.locked_at ? "the locked ground truth" : x.ground_truth?.draft_rows ? "the ground-truth draft" : null,
  ].filter(Boolean);
  const current = x.id === state.session?.id;
  let dlg = $("#deldlg");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "deldlg"; dlg.className = "vdlg deldlg";
    dlg.addEventListener("click", (ev) => { if (ev.target === dlg || ev.target.closest("[data-delclose]")) dlg.close(); });
    document.body.append(dlg);
  }
  dlg.innerHTML = `<form method="dialog" class="vdlg-in" id="delform">
    <h3 style="margin:0">Delete “${esc(x.name)}”?</h3>
    <p class="warn-text" style="margin:0">This cannot be undone. The folder <code>uploads/${esc(x.id)}/</code> is removed with everything in it:</p>
    <ul style="margin:0">${lost.map((l) => `<li>${esc(l)}</li>`).join("")}</ul>
    ${current ? '<p class="hint" style="margin:0">It is the open campaign: the app closes it first.</p>' : ""}
    <label>Type <b>${esc(x.name)}</b> to confirm
      <input type="text" id="del-name" autocomplete="off" aria-label="Campaign name to confirm"></label>
    <p class="msg" id="del-msg" style="margin:0"></p>
    <div class="row" style="justify-content:flex-end">
      <button type="button" class="btn ghost small" data-delclose>Cancel</button>
      <button type="submit" class="btn danger small" id="del-go" disabled>Delete for good</button></div></form>`;
  const input = dlg.querySelector("#del-name"), go = dlg.querySelector("#del-go");
  input.addEventListener("input", () => { go.disabled = input.value.trim() !== x.name.trim(); });
  dlg.querySelector("#delform").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    if (go.disabled) return;
    go.disabled = true;
    try {
      await api(`/api/sessions/${encodeURIComponent(x.id)}`, { method: "DELETE" });
      dlg.close();
      state.openSessions.delete(x.id);
      if (current) {                         // the open campaign is gone: back to the empty start
        state.session = null; await loadForm(); clearForm(); state.videoId = null; state.card = null;
        await renderHome();
      } else await refreshSessionList();
    } catch (e) { go.disabled = false; dlg.querySelector("#del-msg").textContent = `Not deleted: ${e.message}`; }
  });
  dlg.showModal();
  input.focus();
}

// Every saved session as a toggle: the summary line, and its contents when expanded. Runs of a
// collection (session.json "collection") are grouped under their parent, which is a toggle too.
function sessionList(list) {
  if (!list.length) return '<p class="hint">No saved sessions yet.</p>';
  const groups = new Map(), loose = [];
  for (const x of list) {
    const cid = x.collection?.id;
    if (!cid) { loose.push(x); continue; }
    if (!groups.has(cid)) groups.set(cid, []);
    groups.get(cid).push(x);
  }
  const meta = new Map((state.collections || []).map((c) => [c.id, c]));
  return [...groups].map(([cid, kids]) => collectionGroup(cid, kids, meta.get(cid))).join("")
    + loose.map(sessionItem).join("");
}

function collectionGroup(cid, kids, c) {
  kids.sort((a, b) => (a.collection.index || 0) - (b.collection.index || 0));
  const t = c?.test, scored = kids.filter((k) => k.scored).length;
  const open = state.openCollections.has(cid) || kids.some((k) => k.id === state.session?.id);
  return `<details class="coll" data-collection="${esc(cid)}" ${open ? "open" : ""}>
    <summary><b>${esc(c?.name || kids[0].collection.name)}</b>
      <span class="chip">collection</span>
      <span class="hint">${plural(kids.length, "run")} of ${esc(c?.source_name || c?.source || "another session")} ·
        ${t ? `test: ${t.done} done, ${t.error} failed${t.running ? `, ${t.running} running` : ""}` : "no test run yet"}</span></summary>
    <dl class="features sess-body">
      <dt>Source</dt><dd>${esc(c?.source_name || "—")} · <code>uploads/${esc(c?.source || "?")}/</code> (copied, never changed)</dd>
      <dt>Runs</dt><dd>${plural(kids.length, "run")} · ${scored} with scored videos</dd>
      <dt>Test</dt><dd>${t ? `${esc(t.llm?.provider || "?")} / ${esc(t.llm?.model || "?")} · vision ${esc(t.vision || "?")} ·
        updated ${esc(localTime(t.updated_at) || "—")}${t.report ? " · report in the folder below" : ""}`
        : "not run yet (scripts/run_collection_test.py)"}</dd>
      <dt>Saved in</dt><dd><code>uploads/_collections/${esc(cid)}/</code></dd>
    </dl>
    <div class="row">${c ? `<button class="btn small" data-open-collection="${esc(cid)}">Open dashboard</button>
      <button class="btn ghost small" data-coll-tab="reviews" data-cid="${esc(cid)}">Reviews</button>` : ""}</div>
    <div class="coll-kids">${kids.map(sessionItem).join("")}</div>
  </details>`;
}

function sessionItem(x) {
    const current = x.id === state.session?.id;
    const r = x.rows || {};
    return `<details class="sess${current ? " current" : ""}" data-session="${esc(x.id)}" ${state.openSessions?.has(x.id) ? "open" : ""}>
      <summary><b>${esc(x.name)}</b>
        ${current ? '<span class="chip SCALE">open now</span>' : ""}
        <span class="chip ${x.generated ? "" : "muted"}">${x.generated ? "dataset generated" : "draft"}</span>
        <span class="hint">${plural(x.videos_in_form, "video")} · updated ${esc(localTime(x.updated_at) || "—")}</span></summary>
      <dl class="features sess-body">
        <dt>Campaign</dt><dd>${esc(x.campaign?.brand || "—")} · ${esc(x.campaign?.campaign_id || "no id yet")}</dd>
        <dt>Details</dt><dd>${esc([x.campaign?.brand_category, label(x.campaign?.product_type || ""),
          x.campaign?.target_cac ? `target CAC $${x.campaign.target_cac}` : "", x.campaign?.target_countries].filter((v) => v && v !== "—").join(" · ") || "—")}</dd>
        <dt>Form rows</dt><dd>${plural(r["creators.csv"] || 0, "creator")} ·
          ${plural(r["videos.csv"] || 0, "video")} · ${plural(r["comments.csv"] || 0, "comment")}</dd>
        <dt>Pipeline</dt><dd>${x.scored} of ${x.videos_in_form} videos scored · ${plural(x.recaps, "recap")}</dd>
        <dt>Decisions</dt><dd>${plural(x.decisions, "review decision")} logged</dd>
        <dt>Ground truth</dt><dd>${x.ground_truth?.locked_at ? `🔒 locked ${esc(localTime(x.ground_truth.locked_at))}`
          : x.ground_truth?.draft_rows ? `draft, ${plural(x.ground_truth.draft_rows, "video")} filled in, not locked` : "not started"}</dd>
        <dt>Created</dt><dd>${esc(localTime(x.created_at) || "—")}</dd>
        <dt>Generated</dt><dd>${esc(localTime(x.generated_at) || (x.generated ? "yes" : "not yet"))}</dd>
        <dt>Saved in</dt><dd><code>uploads/${esc(x.id)}/session.json</code></dd>
      </dl>
      <div class="row"><button class="btn small" data-open-session="${esc(x.id)}" data-scored="${+x.scored || 0}">${current ? "Go to this session" : "Open this session"}</button>
        ${current ? `<a class="btn small" href="/api/sessions/${encodeURIComponent(x.id)}/prompt-pack" download
          title="One Markdown file to give another AI for improving the P6 prompt: brief, current P6, evaluation, each video's input, answer, human decisions and real outcome">Prompt pack for AI</a>`
          : '<span class="hint" title="The pack rebuilds each video\'s exact P6 input, which needs the session open">Open to get the prompt pack</span>'}
        <a class="btn ghost small" href="/api/sessions/${encodeURIComponent(x.id)}/export" download
          title="ZIP: results.csv, decision log, scores and features JSON, dataset; ground truth and evaluation once locked">Download results</a>
        <a class="btn ghost small" href="/api/sessions/${encodeURIComponent(x.id)}/export?media=true" download
          title="Same ZIP plus clips, audio, keyframes and recap videos (can be large)">+ media</a>
        <button class="btn ghost small" data-copy-session="${esc(x.id)}" title="Duplicate everything: form, clips, scores, reviews, ground truth">Copy session</button>
        <button class="btn danger small" data-delete-session="${esc(x.id)}">Delete session</button>
        <span class="hint" data-copy-msg="${esc(x.id)}"></span></div>
    </details>`;
}

// Open a session and land on its results: Evaluation when it has scored videos (or when the caller asks for
// it, as the parent's dashboard does), else Dataset, where there is nothing to review yet. Review and Recap
// stay one click away in the session tabs.
async function openSession(id, then, scored) {
  await flushDraft();
  try {
    const s = await api(`/api/sessions/${encodeURIComponent(id)}/open`, { method: "POST" });
    state.session = s;
    await loadForm();            // the landing screen never loaded the form schema; setForm needs it
    setForm(s.form);
    state.videoId = null; state.card = null;
    state.runPrompt = "";        // a prompt chosen for the last session may not exist here
    const land = then === "eval" || scored > 0 ? "eval" : "setup";
    setView(land);
    await Promise.all([land === "eval" ? renderEval() : renderSetup(), loadList()]);
  } catch (e) {
    const m = $("#home-msg") || $("#coll-msg");
    if (m) m.textContent = `Could not open: ${e.message}`; else setSaveStatus(`Could not open: ${e.message}`);
  }
}

// Landing screen: the parent sessions (collections, and sessions on their own). Everything else
// (Dataset, Review, Evaluation, Recap) is inside a session, reached from here.
async function renderHome() {
  setView("home");
  clearInterval(state.timerId);
  main.innerHTML = `<p class="empty">Loading…</p>`;
  let sessions, colls;
  try { [sessions, colls] = await Promise.all([api("/api/sessions"), api("/api/collections").catch(() => [])]); }
  catch (e) { main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  if (state.view !== "home") return;
  state.collections = colls;
  const loose = sessions.filter((x) => !x.collection?.id);
  main.innerHTML = `<section class="panel"><h3>Sessions</h3>
      <div class="row"><button class="btn small" id="home-new">New session</button>
        <span class="hint" id="home-msg"></span></div>
      <p class="hint">A session is one campaign. A parent session (collection) groups several child sessions, runs of the
        same campaign; open it to see the evaluation of all of them, or open a child to review, evaluate and recap it.
        Opening a single session goes straight inside it.</p>
      <div id="session-list">${sessionList(sessions)}</div></section>
    <section class="panel"><h3>New parent session</h3>
      ${loose.length ? `<div class="row">
        <select id="coll-source" aria-label="Session to repeat">${loose.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select>
        <input type="text" id="coll-name" placeholder="Parent name" aria-label="Parent session name" maxlength="100">
        <input type="number" id="coll-n" value="3" min="0" max="1000" aria-label="Child sessions" style="width:70px">
        <button class="btn small" id="coll-create">Create with child sessions</button></div>
        <p class="hint">Each child is a full copy of the chosen session (the original is only read). Run the batch on them with
          <code>scripts/run_collection_test.py</code>.</p><p class="msg" id="coll-msg"></p>`
        : `<p class="hint">Save a session first; a parent is made from one.</p>`}</section>`;
}

// A parent session has its own route: #/collections/<id> (Dashboard) and #/collections/<id>/reviews (Reviews),
// so reload, back and a pasted link all land on it. The menu switches between the two.
const COLL_TABS = { dashboard: "Dashboard", reviews: "Reviews" };
const collRoute = (cid, tab) => `#/collections/${encodeURIComponent(cid)}${tab === "reviews" ? "/reviews" : ""}`;
function parseCollRoute(hash) {
  const m = /^#\/collections\/([^/]+)(\/reviews)?\/?$/.exec(hash || "");
  if (!m) return null;
  try { return { cid: decodeURIComponent(m[1]), tab: m[2] ? "reviews" : "dashboard" }; }
  catch (_) { return null; }                      // malformed %-escape: not a collection route, home renders
}

// Dashboard: the evaluation of every child (cached scores, no model call). Reviews: every child's submitted reviews.
async function openCollection(cid, tab = "dashboard") {
  setView("coll");
  clearInterval(state.timerId);
  const mine = state.coll = { cid, tab };
  if (location.hash !== collRoute(cid, tab)) location.hash = collRoute(cid, tab);   // hashchange sees state.coll is current: no reload
  main.innerHTML = `<p class="empty">${tab === "reviews" ? "Loading the reviews of the child sessions…" : "Evaluating child sessions…"}</p>`;
  const base = `/api/collections/${encodeURIComponent(cid)}`;
  let r;
  state.collUnique ??= true;                       // repeats are the norm (every run starts as a copy), so unique first
  try { r = await api(`${base}/${tab === "reviews" ? `reviews?unique=${state.collUnique}` : "evaluation"}`); }
  catch (e) { if (state.coll === mine) main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  if (state.view !== "coll" || state.coll !== mine) return;
  main.innerHTML = `<section class="panel"><div class="row"><button class="btn ghost small" id="coll-back">← Sessions</button>
      <b class="session-title">${esc(r.name)}</b></div>${collectionMenu(cid, tab)}<p class="msg" id="coll-msg"></p>
      ${tab === "reviews" ? "" : collectionDashboard(r)}</section>${tab === "reviews" ? collectionReviews(r) : ""}`;
}

function collectionMenu(cid, tab) {
  return `<nav class="tabs" role="tablist" aria-label="Parent session">${Object.entries(COLL_TABS).map(([k, name]) =>
    `<button class="tab${k === tab ? " active" : ""}" role="tab" data-coll-tab="${k}" data-cid="${esc(cid)}">${name}</button>`).join("")}</nav>`;
}

function routeFromHash() {
  if (location.hash === "#/prompts") { if (state.view !== "prompts") openPromptLibrary(); return; }
  const r = parseCollRoute(location.hash);
  if (r) { if (state.view !== "coll" || state.coll?.cid !== r.cid || state.coll.tab !== r.tab) openCollection(r.cid, r.tab); }
  else if (state.view === "coll" || state.view === "prompts") renderHome();
}
window.addEventListener("hashchange", routeFromHash);

function refIds(file) {
  const key = REF_KEY[file];
  return [...new Set(form.data[file].map((r) => String(r[key] ?? "").trim()).filter(Boolean))];
}

function refOptions(file, v) {
  const ids = refIds(file);
  const missing = v && !ids.includes(v) ? `<option selected value="${esc(v)}">${esc(v)} (not found)</option>` : "";
  return `<option value=""></option>${missing}${ids.map((id) =>
    `<option ${id === v ? "selected" : ""}>${esc(id)}</option>`).join("")}`;
}

function formCell(file, i, c, v) {
  v = v ?? "";
  const err = form.errors.find((e) => e.file === file && e.row === i && e.column === c.name);
  const a = `data-file="${esc(file)}" data-row="${i}" data-col="${esc(c.name)}" class="fc k-${c.kind}${err ? " invalid" : ""}"
    aria-label="${esc(c.label)}, row ${i + 1}"${err ? ` title="${esc(err.message)}" aria-invalid="true"` : ""}`;
  if (c.kind === "enum") return `<select ${a}><option value=""></option>${c.options.map((o) =>
    `<option ${o === String(v).toLowerCase() ? "selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
  if (c.kind === "ref") return `<select ${a} data-ref="${esc(c.options)}">${refOptions(c.options, String(v))}</select>`;
  if (c.kind === "long") return `<textarea ${a} rows="1" placeholder="${esc(c.hint)}">${esc(v)}</textarea>`;
  const mode = c.kind === "int" ? ' inputmode="numeric"' : c.kind === "float" ? ' inputmode="decimal"' : "";
  if (c.name === "video_url") return `<div class="url-cell"><input type="text" ${a} value="${esc(v)}"
      placeholder="${esc(c.hint.replace(/^optional: /, ""))}">
    <label class="btn ghost small" title="Upload a video from this computer; its file:// URL fills the field">Upload…
      <input type="file" class="url-file" data-file="${esc(file)}" data-row="${i}" accept="video/*" hidden
        aria-label="Upload video file, row ${i + 1}"></label></div>`;
  return `<input type="text" ${a}${mode} value="${esc(v)}" placeholder="${esc(c.hint.replace(/^e\.g\. /, ""))}"
    ${c.suggest ? `list="dl-${esc(file)}-${esc(c.name)}"` : ""}>`;
}

function formSection(file) {
  const spec = form.schema.files[file], cols = formCols(file), rows = form.data[file];
  const req = form.schema.required.includes(file);
  const errs = form.errors.filter((e) => e.file === file);
  return `<details class="ftable" data-file="${esc(file)}" ${req || rows.length || errs.length ? "open" : ""}>
    <summary><b>${esc(spec.title)}</b> <code>${esc(file)}</code> ${req ? '<span class="chip">required</span>' : '<span class="chip muted">optional</span>'}
      <span class="hint">· one row per ${esc(spec.one_row_per)} · ${rows.length} row${rows.length === 1 ? "" : "s"}</span>
      ${errs.length ? `<span class="chip DO_NOT_SCALE">${errs.length} to fix</span>` : ""}</summary>
    ${cols.filter((c) => c.suggest).map((c) => `<datalist id="dl-${esc(file)}-${esc(c.name)}">
      ${c.suggest.map((o) => `<option value="${esc(o)}">`).join("")}</datalist>`).join("")}
    ${rows.length ? `<div class="fwrap"><table class="form">
      <tr><th>#</th>${cols.map((c) => `<th title="${esc(c.hint)}">${esc(c.label)}${c.required ? ' <span class="req" aria-label="required">*</span>' : ""}</th>`).join("")}<th></th></tr>
      ${rows.map((r, i) => `<tr><td class="num">${i + 1}</td>${cols.map((c) => `<td>${formCell(file, i, c, r[c.name])}</td>`).join("")}
        <td><button class="btn ghost small" data-delrow="${i}" data-file="${esc(file)}" aria-label="Delete row ${i + 1}">✕</button></td></tr>`).join("")}
    </table></div>` : ""}
    ${errs.length ? `<ul class="ferrs">${errs.map((e) => `<li>${e.row != null ? `Row ${e.row + 1}: ` : ""}${esc(e.message)}</li>`).join("")}</ul>` : ""}
    <div class="row" style="margin-top:8px"><button class="btn ghost small" data-addrow="${esc(file)}">+ Add row</button>
      ${rows.length > 1 ? `<button class="btn ghost small" data-dupvideo="${esc(file)}">+ Add row, same IDs as last</button>` : ""}</div>
    ${BULK_FILES.includes(file) ? bulkPanel(file) : ""}
  </details>`;
}

// Bulk add, one panel per table: paste rows copied from a spreadsheet (tab-separated) or a CSV,
// or upload a file. A header row with the data-template/ column names maps the columns in any order;
// without one, columns are in template order. Rows whose ID is already in the form replace it.
// Comments keep their quick mode: headerless lines are text ⇥ username ⇥ likes ⇥ posted at, for the chosen video.
const BULK_FILES = ["creators.csv", "videos.csv", "comments.csv"];
const BULK_COLS = ["text", "username", "like_count", "timestamp"];

function bulkPanel(file) {
  const cols = form.schema.files[file].cols.map((c) => c.name).join(", ");
  const comments = file === "comments.csv";
  return `<div class="bulk"><h4>Bulk add</h4>
    <p class="hint">Paste rows from a spreadsheet or a CSV, header row first (<code>${esc(cols)}</code>), or upload a
      <code>${esc(file)}</code>. Rows with an ID already in the table replace it.${comments
        ? " Without a header: one comment per line, or text ⇥ username ⇥ likes ⇥ posted at (UTC), for the video chosen here." : ""}</p>
    ${comments ? `<div class="row"><label>Video <select id="bulk-video" aria-label="Video for comments without a video_id">${refOptions("videos.csv", state.bulkVideo || "")}</select></label></div>` : ""}
    <textarea data-bulktext="${esc(file)}" rows="5" aria-label="Rows to add to ${esc(file)}"
      placeholder="${comments ? "One comment per line, or rows with a header" : `${esc(cols.split(", ").slice(0, 4).join(","))},…`}"></textarea>
    <div class="row"><button class="btn ghost small" data-bulkadd="${esc(file)}">Add rows</button>
      <label class="btn ghost small">Upload ${esc(file)}…
        <input type="file" data-bulkfile="${esc(file)}" accept=".csv,.tsv,.txt,text/csv,text/tab-separated-values" hidden></label>
      <span class="hint" data-bulkmsg="${esc(file)}"></span></div></div>`;
}

// Rows of pasted or uploaded text as [{column: value}], plus the header columns this table does not have.
function bulkRows(file, text) {
  const all = form.schema.files[file].cols.map((c) => c.name);
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/).filter((l) => l.trim());
  if (!lines.length) return { rows: [], unknown: [] };
  const sep = lines[0].includes("\t") ? "\t" : ",";
  const first = parseDelimited(lines[0], sep)[0].map((h) => h.trim().toLowerCase());
  if (first.some((h) => all.includes(h))) {
    return { rows: parseDelimited(text, sep, true).map((r) => Object.fromEntries(
               Object.entries(r).map(([k, v]) => [k.trim().toLowerCase(), v]))),
             unknown: first.filter((h) => h && !all.includes(h)) };
  }
  if (file === "comments.csv") {         // quick mode: the text first, for the chosen video
    return { rows: lines.map((l) => {
      const parts = l.includes("\t") ? l.split("\t") : [l];
      return Object.fromEntries(BULK_COLS.map((c, i) => [c, parts[i] ?? ""]));
    }), unknown: [] };
  }
  return { rows: parseDelimited(text, sep).map((r) => Object.fromEntries(all.map((c, i) => [c, r[i] ?? ""]))), unknown: [] };
}

function addBulk(file, text) {
  const msg = main.querySelector(`[data-bulkmsg="${CSS.escape(file)}"]`);
  const say = (m) => { const el = main.querySelector(`[data-bulkmsg="${CSS.escape(file)}"]`); if (el) el.textContent = m; };
  let { rows: incoming, unknown } = bulkRows(file, text);
  if (!incoming.length) { msg.textContent = "Paste or upload at least one row."; return; }
  const cols = formCols(file), key = REF_KEY[file], notes = [];
  if (file === "comments.csv") {
    const vid = $("#bulk-video")?.value || "";
    const missing = incoming.filter((r) => !String(r.video_id ?? "").trim());
    if (missing.length && !vid) { msg.textContent = "Choose the video for rows without a video_id (add it in Videos)."; return; }
    missing.forEach((r) => { r.video_id = vid; });
    incoming.forEach((r) => { if (r.username) r.username = String(r.username).trim().replace(/^@/, ""); });
  }
  if (file === "videos.csv") {           // a session is one campaign: other campaigns' videos stay out
    const cid = String(campaign().campaign_id ?? "").trim();
    const other = incoming.filter((r) => String(r.campaign_id ?? "").trim() && r.campaign_id.trim() !== cid);
    if (other.length) notes.push(`${other.length} row(s) of other campaigns left out`);
    incoming = incoming.filter((r) => !other.includes(r));
  }
  const rows = form.data[file];
  const blank = (r) => !Object.values(r).some((v) => String(v ?? "").trim());
  for (let i = rows.length - 1; i >= 0; i--) if (blank(rows[i])) rows.splice(i, 1);
  const same = (a, b) => cols.every((c) => String(a[c.name] ?? "").trim() === String(b[c.name] ?? "").trim());
  let added = 0, replaced = 0, dupes = 0;
  for (const r of incoming) {
    const row = Object.fromEntries(cols.filter((c) => c.name in r).map((c) => [c.name, String(r[c.name] ?? "").trim()]));
    if (blank(row)) continue;
    if (!key && rows.some((x) => same(x, { ...blankRow(file), ...row }))) { dupes++; continue; }   // comments: no ID, skip exact repeats
    const at = key && row[key] ? rows.findIndex((x) => String(x[key] ?? "").trim() === row[key]) : -1;
    if (at >= 0) { rows[at] = { ...rows[at], ...row }; replaced++; }
    else { rows.push({ ...blankRow(file), ...row }); added++; }
  }
  if (dupes) notes.push(`${dupes} already in the table skipped`);
  if (unknown.length) notes.push(`ignored columns: ${unknown.join(", ")}`);
  form.errors = form.errors.filter((e) => e.file !== file);   // row numbers may have shifted
  saveDraft(); renderForm();
  say(`Added ${plural(added, "row")}${replaced ? `, replaced ${replaced}` : ""}.${notes.length ? ` ${notes.join("; ")}.` : ""}`);
}

function campaignCard() {
  const cols = formCols(CAMPAIGN), errs = form.errors.filter((e) => e.file === CAMPAIGN);
  return `${cols.filter((c) => c.suggest).map((c) => `<datalist id="dl-${CAMPAIGN}-${esc(c.name)}">
      ${c.suggest.map((o) => `<option value="${esc(o)}">`).join("")}</datalist>`).join("")}
    <div class="card-form">${cols.map((c) => `<label><span>${esc(c.label)}${c.required ? ' <span class="req" aria-label="required">*</span>' : ""}</span>
      ${formCell(CAMPAIGN, 0, c, campaign()[c.name]).replace(/placeholder="[^"]*"/, "")}<small>${esc(c.hint)}</small></label>`).join("")}</div>
    ${errs.length ? `<ul class="ferrs">${errs.map((e) => `<li>${esc(e.message)}</li>`).join("")}</ul>` : ""}`;
}

function renderForm() {
  const card = $("#ds-campaign");
  if (card) card.innerHTML = campaignCard();
  const el = $("#ds-form");
  if (!el) return;
  if (!campaignReady()) {
    el.innerHTML = '<p class="locked">Fill in the campaign ID and brand above first. Creators, videos and comments for this campaign come next.</p>';
    $("#generate")?.setAttribute("disabled", "");
    return;
  }
  $("#generate")?.removeAttribute("disabled");
  const open = Object.fromEntries([...el.querySelectorAll("details.ftable")].map((d) => [d.dataset.file, d.open]));
  el.innerHTML = Object.keys(form.schema.files).filter((f) => f !== CAMPAIGN).map(formSection).join("");
  // keep the sections the user opened or closed as they were
  el.querySelectorAll("details.ftable").forEach((d) => {
    if (open[d.dataset.file] != null && !form.errors.some((e) => e.file === d.dataset.file)) d.open = open[d.dataset.file];
  });
}

// Ids typed in one table feed the dropdowns of the others.
function refreshRefs() {
  main.querySelectorAll("select[data-ref]").forEach((sel) => {
    const v = form.data[sel.dataset.file][+sel.dataset.row][sel.dataset.col] ?? "";
    sel.innerHTML = refOptions(sel.dataset.ref, String(v));
  });
  const bulk = $("#bulk-video");
  if (bulk) bulk.innerHTML = refOptions("videos.csv", bulk.value);
}

// RFC 4180 CSV: quoted fields, doubled quotes, newlines inside quotes. Rows as arrays, or as
// {header: value} objects when the first row is the header.
function parseDelimited(text, sep = ",", header = false) {
  const rows = [];
  let row = [], field = "", q = false;
  text = text.replace(/^\uFEFF/, "");
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (q) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') q = false;
      else field += ch;
    } else if (ch === '"' && !field) q = true;
    else if (ch === sep) { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); rows.push(row); row = []; field = "";
    } else field += ch;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  const kept = rows.filter((r) => r.some((x) => x.trim()));
  if (!header) return kept;
  const [head = [], ...body] = kept;
  return body.map((r) => Object.fromEntries(head.map((h, i) => [h.trim(), r[i] ?? ""])));
}

const parseCsv = (text) => parseDelimited(text, ",", true);

async function importCsvs(files) {
  const done = [], skipped = [], generated = [], parsed = {};
  for (const f of files) {
    const name = f.name.toLowerCase();
    if (!form.schema.files[name]) { (GENERATED.includes(name) ? generated : skipped).push(f.name); continue; }
    parsed[name] = parseCsv(await f.text());
  }
  // A session is one campaign: keep this session's campaign (or the file's first) and only its rows.
  let other = [];
  if (parsed[CAMPAIGN]) {
    const keep = parsed[CAMPAIGN].find((r) => r.campaign_id === campaign().campaign_id) || parsed[CAMPAIGN][0];
    other = parsed[CAMPAIGN].filter((r) => r !== keep).map((r) => r.campaign_id);
    parsed[CAMPAIGN] = keep ? [keep] : [];
  }
  const cid = parsed[CAMPAIGN]?.[0]?.campaign_id || campaign().campaign_id;
  if (parsed["videos.csv"] && cid && parsed["videos.csv"].some((r) => r.campaign_id)) {
    parsed["videos.csv"] = parsed["videos.csv"].filter((r) => r.campaign_id === cid);
    const vids = new Set(parsed["videos.csv"].map((r) => r.video_id)), crs = new Set(parsed["videos.csv"].map((r) => r.creator_id));
    if (parsed["comments.csv"]) parsed["comments.csv"] = parsed["comments.csv"].filter((r) => vids.has(r.video_id));
    if (parsed["creators.csv"]) parsed["creators.csv"] = parsed["creators.csv"].filter((r) => crs.has(r.creator_id));
  }
  for (const [name, rows] of Object.entries(parsed)) {
    const cols = formCols(name);
    form.data[name] = rows.map((r) => Object.fromEntries(cols.map((c) => [c.name, r[c.name] ?? ""])));
    done.push(`${name} (${form.data[name].length})`);
  }
  if (!form.data[CAMPAIGN].length) form.data[CAMPAIGN].push(blankRow(CAMPAIGN));
  form.errors = [];
  saveDraft();
  renderForm();
  $("#form-msg").textContent = [done.length ? `Imported into the form: ${done.join(", ")}. Check it, then generate.` : "",
    other.length ? `Other campaigns in the file were left out (${other.join(", ")}): start a new session for each.` : "",
    generated.length ? `Skipped ${generated.join(", ")}: the pipeline produces these from each video.` : "",
    skipped.length ? `Not a form table, skipped: ${skipped.join(", ")} (answer keys go in the Evaluation tab).` : ""].join(" ");
}

async function generateDataset() {
  const btn = $("#generate"), msg = $("#form-msg");
  btn.disabled = true;
  msg.textContent = "Checking and building the dataset…";
  try {
    clearTimeout(state.saveId); state.dirty = false;
    await state.saving;
    await api("/api/dataset", { method: "POST", headers: { "content-type": "application/json" }, body: draftBody() });
    state.session = await api("/api/session");
    refreshCrumb();
    form.errors = [];
    state.videoId = null; state.card = null;
    await Promise.all([renderSetup(), loadList()]);
    $("#form-msg").textContent = "Dataset created. Add a video for each row below, then start the pipeline.";
  } catch (e) {
    form.errors = e.detail?.errors || [];
    state.dirty = true; flushDraft();          // invalid rows are still remembered
    renderForm();
    msg.textContent = form.errors.length ? `Not created: ${form.errors.length} problem(s), marked in red below.` : `Not created: ${e.message}`;
    btn.disabled = false;
  }
}

async function resetDataset() {
  try {
    await flushDraft();
    await api("/api/dataset/reset", { method: "POST" });
    state.session = null;
    refreshCrumb();
    await loadForm();
    clearForm();
    state.videoId = null; state.card = null;
    await Promise.all([renderSetup(), loadList()]);
  } catch (e) { setSaveStatus(`Could not start a new session: ${e.message}`); }
}

async function renderSetup() {
  main.innerHTML = `<p class="empty">Loading…</p>`;
  let ds, vids, job;
  try {
    [ds, vids, job] = await Promise.all([api("/api/dataset"), listVideos(),
      api("/api/pipeline/status"), loadForm()]);
    state.promptList = await api("/api/prompts").catch(() => null);
    if (state.runPrompt && !(state.promptList?.versions || []).some((v) => v.id === state.runPrompt)) state.runPrompt = "";
  } catch (e) { main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  if (state.view !== "setup") return;
  state.job = job;
  state.session = ds.session;
  refreshCrumb();
  const active = ds.files.includes("videos.csv");
  const s = ds.session;
  main.innerHTML = `
    <section class="panel"><h3>Session</h3>
      <div class="row">
        <b id="session-title" class="session-title">${esc(s?.name || "No campaign open")}</b>
        <button class="btn ghost small" id="ds-reset" ${s ? "" : "disabled"}>New campaign</button>
      </div>
      <p class="hint" id="session-status">${s ? `Saved ${esc(localTime(s.updated_at))} · uploads/${esc(s.id)}/session.json`
        : "Each session is one campaign. A new one starts, and is saved, as soon as you type below."}</p>
      <p class="hint">Everything typed is kept in the session's <code>session.json</code>; its video URLs, clips,
        pipeline results and review decisions are kept in the same folder. Other sessions are listed under Sessions.</p></section>
    <section class="panel"><h3>1 · Campaign</h3>
      <div class="row">
        <label class="btn ghost small">Import from CSV files…
          <input type="file" id="ds-import" multiple accept=".csv,text/csv" hidden></label>
        <button class="btn ghost small" id="form-clear">Clear form</button>
      </div>
      <p class="hint">Same columns as the files in <code>data-template/</code>. Leave a value blank when you don't
        know it, never 0. <span class="req">*</span> = required. Ratios left blank are computed when possible.</p>
      <div id="ds-campaign"></div></section>
    <section class="panel"><h3>2 · Creators, videos and comments</h3>
      <p>${active ? `Generated: <b>${ds.videos} video${ds.videos === 1 ? "" : "s"}</b> · ${esc(ds.files.join(", "))}`
        : "<b>No dataset generated yet.</b> Add this campaign's creators, videos and comments, then generate the dataset."}</p>
      <p class="hint">Every video belongs to this campaign. Creator IDs must match between tables. Hover a column
        title for help. Media details, keyframes and transcripts are not entered here: the pipeline produces them from each video.</p>
      <div id="ds-form"></div>
      <div class="row" style="margin-top:14px">
        <button class="btn" id="generate">${active ? "Regenerate dataset (re-scores every video)" : "Generate dataset"}</button>
      </div>
      <p id="form-msg" class="msg"></p></section>
    ${active ? `<section class="panel"><h3>3 · Video files</h3>
      <table class="urls"><tr><th>Video</th><th>Brand · creator</th><th>Video URL</th><th>or upload the file</th><th>Status</th></tr>
      ${vids.map((v) => `<tr><td>${esc(v.video_id)}</td><td>${esc(v.brand)} · @${esc(v.creator)}</td>
        <td><input type="url" class="url" data-id="${esc(v.video_id)}" value="${esc(v.source_url || "")}"
          placeholder="https://… (mp4 link, or Instagram / TikTok / YouTube page)" aria-label="Video URL for ${esc(v.video_id)}"></td>
        <td><input type="file" class="clip" data-id="${esc(v.video_id)}" accept="video/*" aria-label="Video file for ${esc(v.video_id)}"></td>
        <td class="st" data-id="${esc(v.video_id)}">${videoStatus(v, job)}</td></tr>`).join("")}</table>
      <p class="hint">Each video needs a URL or a file: the pipeline extracts its keyframes, transcript and
        on-screen text from it. Without one, the video is scored on its metrics and comments only.</p></section>
    <section class="panel"><h3>4 · Run</h3>
      <div id="llm-picker"><p class="hint" style="margin-top:0">Loading models…</p></div>
      <div class="row" style="margin-top:12px">
        <button class="btn" id="start" ${job.running ? "disabled" : ""}>Start pipeline</button>
        <label><input type="checkbox" id="force"> Re-run videos that are already scored</label>
        <label>Prompt for the next run <select id="run-prompt" title="Which prompt version the next run scores with. Choosing it does not adopt it.">
          <option value="">Active (${esc(state.promptList?.active || "P6")})</option>
          ${(state.promptList?.versions || []).filter((v) => v.id !== state.promptList.baseline).map((v) =>
            `<option value="${esc(v.id)}" ${state.runPrompt === v.id ? "selected" : ""}>${esc(v.id)}${v.builtin ? " (built in)" : ""}</option>`).join("")}
        </select></label>
      </div>
      <p id="run-line" class="hint"></p>
      <p id="job-msg" class="msg">${esc(jobLine(job))}</p></section>` : ""}`;
  renderForm();
  state.vids = vids;
  if (active) { renderLlmPicker(); refreshRunLine(); }
  if (job.running) poll();
}

// ------------------------------------------------------------------ which LLM runs the pipeline
// The choice is saved per session. Each provider + model keeps its own cached answers, so the
// statuses, cards and evaluation follow the chosen model, and switching back costs nothing.
const visionNote = (v) => (v === true ? "reads images" : v === false ? "text only: no keyframe analysis" : "");

// The two common ways to run: this machine's Claude Code (subscription login) or the Anthropic API.
// Availability comes from /api/llm (no model call): an option that cannot run is disabled with the reason.
// Whether the CLI is actually logged in is only known on the first run. Never shows a key, only its env name.
const LLM_TWO = [["claude_code", "This machine — Claude Code (subscription)"], ["anthropic", "Anthropic API"]];
function llmReason(p) {
  if (!p) return "not available";
  if (p.missing_sdk) return `Python package missing: ${p.missing_sdk}`;
  if (!p.configured) return p.id === "claude_code" ? "the claude CLI was not found (install Claude Code and log in, or set CLAUDE_CODE_BIN)" : `${p.env} is not set`;
  return p.id === "claude_code" ? "claude CLI found; login is checked on the first run" : "API key present";
}
function llmTwoChoice(info, running) {
  const cur = info.current.provider;
  return `<fieldset class="llm-two" aria-label="Where the model runs"><legend>Run on</legend>
    ${LLM_TWO.map(([id, name]) => {
      const p = info.providers.find((x) => x.id === id), ok = p && p.configured && !p.missing_sdk;
      return `<label class="llm-opt${ok ? "" : " off"}" title="${esc(llmReason(p))}"><input type="radio" name="llm-which" value="${esc(id)}"
        ${cur === id ? "checked" : ""} ${ok && !running ? "" : "disabled"}> <b>${esc(name)}</b>
        <span class="hint">${esc(llmReason(p))}</span></label>`;
    }).join("")}
    ${LLM_TWO.some(([id]) => id === cur) ? "" : `<span class="hint">Using ${esc(cur)} (chosen below).</span>`}</fieldset>`;
}

async function renderLlmPicker(provider) {
  const box = $("#llm-picker");
  if (!box) return;
  let info;
  try { info = await api("/api/llm"); } catch (e) { box.innerHTML = `<p class="msg err">Models: ${esc(e.message)}</p>`; return; }
  const cur = info.current, pid = provider || cur.provider;
  const p = info.providers.find((x) => x.id === pid);
  box.innerHTML = `${llmTwoChoice(info, state.job?.running)}<div class="row llm">
      <label>LLM provider <select id="llm-provider">${info.providers.map((x) => `<option value="${esc(x.id)}"
        ${x.id === pid ? "selected" : ""}>${esc(x.label)}${x.configured ? "" : " (not set up)"}</option>`).join("")}</select></label>
      <label>Model <input id="llm-model" list="llm-models" autocomplete="off" placeholder="loading…"
        value="${esc(pid === cur.provider ? cur.model || "" : p.default_model || "")}"></label>
      <datalist id="llm-models"></datalist>
      <button class="btn ghost small" id="llm-save" ${state.job?.running ? "disabled" : ""}>Use this model</button>
    </div>
    <p class="hint" id="llm-msg">Runs on <b>${esc(cur.provider)} / ${esc(cur.model || "no model chosen")}</b>
      (default ${esc(info.default.provider)} / ${esc(info.default.model || "none")}). <span id="llm-list"></span></p>`;
  const r = await api(`/api/llm/models?provider=${encodeURIComponent(pid)}`).catch((e) => ({ models: [], error: e.message }));
  if ($("#llm-provider")?.value !== pid) return;             // the provider changed meanwhile
  $("#llm-models").innerHTML = r.models.map((m) => `<option value="${esc(m.id)}">${esc(visionNote(m.vision))}</option>`).join("");
  $("#llm-model").placeholder = r.models.length ? "choose or type a model id" : "type a model id";
  const list = $("#llm-list");
  list.className = r.error ? "err" : "";
  list.textContent = r.error ? `Model list unavailable: ${r.error}`
    : r.models.length ? `${r.models.length} model${r.models.length === 1 ? "" : "s"} available.`
    : "No models installed" + (pid === "ollama" ? " (e.g. ollama pull qwen2.5vl)." : ".");
}

// What Start pipeline will do for the prompt chosen for the next run (no model call): the run line + button label.
async function refreshRunLine() {
  const line = $("#run-line"), btn = $("#start");
  if (!line) return;
  let p;
  try { p = await api(`/api/pipeline/plan${state.runPrompt ? `?prompt_id=${encodeURIComponent(state.runPrompt)}` : ""}`); }
  catch (e) { line.textContent = `Run: ${e.message}`; return; }
  const r = p.run;
  line.innerHTML = r ? `This session's run ${String(r.n ?? 1).padStart(3, "0")}${r.inferred ? " (inferred)" : ""}: <b>${esc(r.prompt_id)}</b> on
      ${esc(r.provider)} / ${esc(r.model)} · ${esc(r.status)}${r.scored != null ? ` ${r.scored}/${r.videos}` : ""}.`
    : "No run yet: Start runs the first one in this session.";
  if (btn) btn.textContent = p.button || "Start pipeline";
}

// The two-step confirm before any model call. Returns the mode to send ("auto" | "resume" | "child") or null on cancel.
function startChoice(p, ask) {
  if (p.action === "ask") return ask(p.message_resume) ? "resume" : ask(p.message_child) ? "child" : null;
  return ask(p.message) ? "auto" : null;
}

async function saveLlm(provider) {
  const body = provider ? { provider, model: "" }          // empty model = that provider's default
    : { provider: $("#llm-provider").value, model: $("#llm-model").value.trim() };
  try {
    await api("/api/llm", { method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  } catch (e) {
    await renderLlmPicker();                                  // show what the session really uses, not the radio just clicked
    if ($("#llm-msg")) $("#llm-msg").textContent = `Not changed: ${e.message}`;
    return;
  }
  await renderLlmPicker();
  refreshRunLine();
  const vids = await listVideos();                     // statuses now follow the new model
  for (const v of vids) {
    const td = main.querySelector(`td.st[data-id="${CSS.escape(v.video_id)}"]`);
    if (td) td.innerHTML = videoStatus(v, state.job);
  }
  state.card = null;
  loadList();
}

// Prompt versions and iterations: the tuning loop of the workflow design. The team reads the error
// report, saves a tuned P6 as a new immutable version, runs an iteration (the version and P5 on every
// video, same model), and adopts it only when it beats P5 against the ground truth.
const pvMetric = (m) => (m && m.n ? `${m.accuracy == null ? "—" : `${Math.round(m.accuracy * 100)}%`} · ${m.fp} false SCALE · ${m.tp}/${m.winners} winners` : "—");
const pvBasis = { cac_outcome: "real CAC outcome", expected_scale: "expected verdict" };
// Results screen: ONE measure. The real CAC outcome when any video has one, else the expected verdict (as the adoption gate does).
const evalBasis = (c) => (c.cac_outcome && c.cac_outcome.videos ? "cac_outcome" : "expected_verdict");
const evalBasisName = { cac_outcome: "real CAC outcome", expected_verdict: "expected verdict (scale / don't scale)" };

function pvResult(it) {
  if (!it) return '<span class="hint">no iteration yet</span>';
  if (it.status !== "done") return `<span class="hint">${esc(it.status)}</span>`;
  const b = it.comparison?.beats_baseline;
  return b == null ? `<span class="hint" title="${esc(it.comparison?.why)}">not judged</span>`
    : `<span class="chip ${b ? "SCALE" : "DO_NOT_SCALE"}" title="${esc(it.comparison.why)}">${b ? "beats P5" : "not better"}</span>`;
}

async function renderPrompts() {
  const body = $("#eval-body");
  let pl, its, job;
  try { [pl, its, job] = await Promise.all([api("/api/prompts"), api("/api/iterations"), api("/api/pipeline/status")]); }
  catch (_) { body.innerHTML = `<section class="panel"><p>Open a campaign in the Dataset tab first.</p></section>`; return; }
  state.job = job;
  const latest = {};                         // iterations come newest first
  for (const it of its) if (!(it.prompt_id in latest)) latest[it.prompt_id] = it;
  const busy = job.running;
  const view = state.pvView, ov = state.pvOverride;
  const rows = pl.versions.map((v) => {
    const p6family = v.id !== pl.baseline;
    return `<tr>
      <td><b>${esc(v.id)}</b> ${v.active ? '<span class="chip SCALE">active</span>' : ""}${v.id === pl.baseline ? ' <span class="chip muted">baseline</span>' : ""}</td>
      <td>${esc(v.parent || "—")}</td>
      <td class="pv-note">${esc(v.note || "")}</td>
      <td>${v.created_at ? esc(localTime(v.created_at)) : "built in"}</td>
      <td><code title="SHA-256 ${esc(v.sha256)}">${esc(v.sha256.slice(0, 8))}</code></td>
      <td>${p6family ? pvResult(latest[v.id]) : '<span class="hint">measured in every iteration</span>'}</td>
      <td class="pv-actions">
        <button class="btn ghost small" data-pv="view" data-id="${esc(v.id)}">View</button>
        ${v.parent ? `<button class="btn ghost small" data-pv="diff" data-id="${esc(v.id)}">Diff vs ${esc(v.parent)}</button>` : ""}
        ${p6family ? `<button class="btn small" data-pv="run" data-id="${esc(v.id)}" ${busy ? "disabled" : ""}>Run iteration</button>` : ""}
        ${p6family && !v.active ? `<button class="btn ghost small" data-pv="adopt" data-id="${esc(v.id)}" ${busy ? "disabled" : ""}>Adopt</button>` : ""}
      </td></tr>`;
  }).join("");
  const itRows = its.map((it) => {
    const c = it.comparison, basis = c?.basis;
    return `<tr><td>#${it.n}</td><td><b>${esc(it.prompt_id)}</b>${it.prompt_exists ? "" : ' <span class="hint">(deleted)</span>'}</td>
      <td>${esc(it.llm.provider)} / ${esc(it.llm.model || "")}</td><td>${esc(localTime(it.started_at))}</td>
      <td>${c ? `${c.videos}${it.errors ? ` <span class="hint">(${it.errors} errors)</span>` : ""}` : "—"}</td>
      <td>${basis ? esc(pvBasis[basis]) : "—"}</td>
      <td>${basis ? pvMetric(c.baseline[basis]) : "—"}</td><td>${basis ? pvMetric(c.candidate[basis]) : "—"}</td>
      <td>${pvResult(it)}${it.note ? `<div class="hint">${esc(it.note)}</div>` : ""}</td></tr>`;
  }).join("");
  const adoptions = pl.adoptions.slice(-5).reverse().map((a) => `<li>${esc(localTime(a.at))}: <b>${esc(a.id)}</b>
      ${a.override_reason ? `adopted by override: “${esc(a.override_reason)}”` : a.beats_baseline ? `beat P5 in iteration #${a.iteration}` : "restored as default"}</li>`).join("");

  body.innerHTML = `<section class="panel"><h3>Prompts for this campaign</h3>
      <p class="hint" style="margin-top:0">The review card uses <b>${esc(pl.active)}</b>. Versions are created once in the global
        <button class="linkish" data-pv="library">Prompts</button> area and any campaign can run them; P5 is the fixed baseline and never changes.
        <b>Run iteration</b> scores every video with that version
        <i>and</i> with P5 on the current model (API calls), then freezes the verdicts. A version is adopted only when its latest
        iteration on this model beats P5 against the locked ground truth: never more false SCALE, then better accuracy.
        Anything else needs a written override reason, which is logged.</p>
      <p id="pv-job" class="msg">${busy ? esc(pvJobLine(job)) : ""}</p>
      <div class="tablewrap"><table class="rv"><thead><tr><th>Version</th><th>Parent</th><th>Note</th><th>Created</th><th>Hash</th>
        <th>Latest iteration</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <p id="pv-msg" class="msg"></p></section>
    ${ov ? `<section class="panel"><h3>Adopt ${esc(ov.id)} anyway?</h3><p>${esc(ov.why)}</p>
      <label class="pv-field">Written reason (logged with the adoption)<textarea id="pv-reason" rows="3"></textarea></label>
      <div class="row"><button class="btn danger small" data-pv="force" data-id="${esc(ov.id)}">Adopt with this reason</button>
        <button class="btn ghost small" data-pv="cancel">Cancel</button></div></section>` : ""}
    ${view ? `<section class="panel"><h3>${esc(view.title)}</h3><button class="btn ghost small" data-pv="close">Close</button>
      <pre class="text pv-pre">${esc(view.text)}</pre></section>` : ""}
    <section class="panel"><h3>Iterations</h3>
      ${its.length ? `<div class="tablewrap"><table class="rv"><thead><tr><th>#</th><th>Version</th><th>Model</th><th>Started</th>
        <th>Videos compared</th><th>Measured on</th><th>P5 baseline</th><th>Version</th><th>Result</th></tr></thead>
        <tbody>${itRows}</tbody></table></div>
        <p class="hint">Each cell: accuracy · false SCALE (budget on a loser) · winners caught. Measured now against the ground truth,
          from the verdicts frozen when the iteration ran.</p>`
      : "<p>No iteration yet. Run one from a version above.</p>"}
      ${adoptions ? `<h3 style="margin-top:16px">Recent adoptions</h3><ul class="pv-log">${adoptions}</ul>` : ""}</section>`;
  if (busy) pvPoll();
}

// The global Prompts module: one library of versions for every session (uploads/_prompts/library.json).
async function openPromptLibrary() {
  setView("prompts");
  clearInterval(state.timerId); clearInterval(state.pvPollId);
  if (location.hash !== "#/prompts") location.hash = "#/prompts";
  return renderPromptLibrary();
}

async function renderPromptLibrary() {
  let pl;
  try { pl = await api("/api/prompts"); }
  catch (e) { main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  if (state.view !== "prompts") return;
  const ed = state.pvEdit, view = state.pvView;
  const rows = pl.versions.map((v) => `<tr>
      <td><b>${esc(v.id)}</b>${v.id === pl.baseline ? ' <span class="chip muted">baseline</span>' : v.builtin ? ' <span class="chip muted">built in</span>' : ""}</td>
      <td>${esc(v.parent || "—")}</td><td class="pv-note">${esc(v.note || "")}</td>
      <td>${v.created_at ? esc(localTime(v.created_at)) : "built in"}</td>
      <td><code title="SHA-256 ${esc(v.sha256)}">${esc(v.sha256.slice(0, 8))}</code></td><td class="num">${esc(int(v.chars))}</td>
      <td class="pv-actions">
        <button class="btn ghost small" data-pv="view" data-id="${esc(v.id)}">View</button>
        ${v.parent ? `<button class="btn ghost small" data-pv="diff" data-id="${esc(v.id)}">Diff vs ${esc(v.parent)}</button>` : ""}
        ${v.id !== pl.baseline ? `<button class="btn ghost small" data-pv="new" data-id="${esc(v.id)}">New version from this</button>` : ""}
        ${!v.builtin ? `<button class="btn danger small" data-pv="delete" data-id="${esc(v.id)}">Delete</button>` : ""}
      </td></tr>`).join("");
  main.innerHTML = `<section class="panel"><h3>Prompts</h3>
      <p class="hint" style="margin-top:0">One library for every campaign. A version is saved once, from a parent, and never edited afterwards.
        P5 (the fixed baseline) and P6 are built in and read-only. Saving a version does not score or adopt anything: pick it in a
        campaign's Dataset tab to run it, then compare it with P5 under that campaign's <b>Evaluation → Prompts for this campaign</b>.
        A version a campaign has active cannot be deleted.</p>
      <div class="tablewrap"><table class="rv"><thead><tr><th>Version</th><th>Parent</th><th>Note</th><th>Created</th><th>Hash</th>
        <th>Characters</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>
      <p id="pv-msg" class="msg"></p></section>
    ${ed ? `<section class="panel"><h3>New version from ${esc(ed.parent)}</h3>
      <p class="hint" style="margin-top:0">Keep <code>{video_summary}</code> exactly once: it is where each video's data goes.
        Write any other literal brace doubled, <code>{{</code> or <code>}}</code>, as in P5.</p>
      <label class="pv-field">What changed and why (from the error report)<input id="pv-note" type="text" maxlength="500" value="${esc(ed.note || "")}"></label>
      <textarea id="pv-text" class="pv-text" rows="24" spellcheck="false">${esc(ed.text)}</textarea>
      <div class="row"><button class="btn small" data-pv="save">Save version</button>
        <button class="btn ghost small" data-pv="cancel">Cancel</button></div>
      <p id="pv-edmsg" class="msg"></p></section>` : ""}
    ${view ? `<section class="panel"><h3>${esc(view.title)}</h3><button class="btn ghost small" data-pv="close">Close</button>
      <pre class="text pv-pre">${esc(view.text)}</pre></section>` : ""}`;
}

const pvJobLine = (job) => `${job.kind === "iteration" ? `Iteration #${job.iteration} (${job.prompt_id} + P5)` : "Pipeline"} running: ${job.done}/${job.total} videos…`;

function pvPoll() {
  clearInterval(state.pvPollId);
  state.pvPollId = setInterval(async () => {
    let job;
    try { job = await api("/api/pipeline/status"); } catch (_) { return; }
    if (state.view !== "eval" || state.evalTab !== "prompts") return clearInterval(state.pvPollId);
    if ($("#pv-job")) $("#pv-job").textContent = job.running ? pvJobLine(job) : "";
    if (!job.running) { clearInterval(state.pvPollId); loadList(); renderPrompts(); }
  }, 1500);
}

async function promptAction(action, id, btn) {
  const msg = (t) => { const el = $("#pv-edmsg") || $("#pv-msg"); if (el) el.textContent = t; };
  const json = (method, body) => ({ method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  try {
    if (action === "view") { const v = await api(`/api/prompts/${encodeURIComponent(id)}`); state.pvView = { title: `${v.id} · full text`, text: v.text }; }
    else if (action === "diff") { const d = await api(`/api/prompts/${encodeURIComponent(id)}/diff`); state.pvView = { title: `${id} · changes`, text: d }; }
    else if (action === "close") state.pvView = null;
    else if (action === "new") { const v = await api(`/api/prompts/${encodeURIComponent(id)}`); state.pvEdit = { parent: v.id, text: v.text }; state.pvOverride = null; }
    else if (action === "cancel") { state.pvEdit = null; state.pvOverride = null; }
    else if (action === "save") {
      const ed = state.pvEdit;
      ed.text = $("#pv-text").value; ed.note = $("#pv-note").value;
      btn.disabled = true;
      const v = await api("/api/prompts", json("POST", { parent: ed.parent, text: ed.text, note: ed.note }));
      state.pvEdit = null;
      await rerenderPrompts(); $("#pv-msg").textContent = `Saved ${v.id}. Open a campaign and run an iteration to compare it with P5.`; return;
    }
    else if (action === "run") {
      btn.disabled = true;
      await api("/api/iterations", json("POST", { prompt_id: id }));
    }
    else if (action === "adopt" || action === "force") {
      const reason = action === "force" ? $("#pv-reason").value : "";
      try { await api("/api/prompts/active", json("PUT", { id, override_reason: reason })); state.pvOverride = null; }
      catch (e) {
        if (!/override reason/.test(e.message)) throw e;
        state.pvOverride = { id, why: e.message };
        await rerenderPrompts(); if (action === "force") msg(e.message); return;
      }
      loadList();
    }
    else if (action === "library") return openPromptLibrary();
    else if (action === "delete") {
      btn.disabled = true; await api(`/api/prompts/${encodeURIComponent(id)}`, { method: "DELETE" });
      if (state.runPrompt === id) state.runPrompt = "";
    }
  } catch (e) {
    if (btn) btn.disabled = false;
    msg(`${action === "save" ? "Not saved" : "Failed"}: ${e.message}`); return;
  }
  rerenderPrompts();
}
const rerenderPrompts = () => (state.view === "prompts" ? renderPromptLibrary() : renderPrompts());

function poll() {
  clearInterval(state.pollId);
  state.pollId = setInterval(async () => {
    let job, vids;
    try { [job, vids] = await Promise.all([api("/api/pipeline/status"), listVideos()]); } catch (_) { return; }
    state.job = job;
    const btn = $("#process-all");
    btn.disabled = job.running;
    btn.textContent = job.running ? `Running ${job.done}/${job.total}…` : "Run pipeline";
    if (state.view === "setup") {
      for (const v of vids) {
        const td = main.querySelector(`td.st[data-id="${CSS.escape(v.video_id)}"]`);
        if (td) td.innerHTML = videoStatus(v, job);
      }
      if ($("#job-msg")) $("#job-msg").textContent = jobLine(job);
      if ($("#start")) $("#start").disabled = job.running;
    }
    if (!job.running) {
      clearInterval(state.pollId);
      loadList();
      if (state.view === "review" && state.videoId) openVideo(state.videoId);
    }
  }, 1500);
}

async function startPipeline() {
  const urls = {};
  main.querySelectorAll("input.url").forEach((i) => (urls[i.dataset.id] = i.value.trim()));
  state.runPrompt = $("#run-prompt")?.value || "";
  if ($("#start")) $("#start").disabled = true;
  try {
    const plan = await api(`/api/pipeline/plan${state.runPrompt ? `?prompt_id=${encodeURIComponent(state.runPrompt)}` : ""}`);
    const mode = startChoice(plan, (text) => window.confirm(text));         // nothing is created or scored before a yes
    if (!mode) { if ($("#start")) $("#start").disabled = false; return; }
    const body = { force: !!$("#force")?.checked, urls, prompt_id: state.runPrompt || null, mode };
    const res = await api("/api/pipeline/start", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if (res.created_child && res.session_id) {                                // the new run is its own session: open it
      state.runPrompt = "";
      await openSession(res.session_id, "setup");
      return;
    }
    state.job = res;
    if ($("#job-msg")) $("#job-msg").textContent = jobLine(state.job);
    refreshRunLine();
    poll();
  } catch (e) {
    if ($("#job-msg")) $("#job-msg").textContent = `Not started: ${e.message}`;
    if ($("#start")) $("#start").disabled = false;
  }
}

async function uploadClip(input) {
  const id = input.dataset.id, f = input.files[0];
  const td = main.querySelector(`td.st[data-id="${CSS.escape(id)}"]`);
  if (!f) return;
  td.innerHTML = '<span class="busy">uploading…</span>';
  const body = new FormData();
  body.append("file", f);
  try {
    await api(`/api/videos/${encodeURIComponent(id)}/clip`, { method: "POST", body });
    const url = main.querySelector(`input.url[data-id="${CSS.escape(id)}"]`);
    if (url) url.value = "";                 // the uploaded file replaces the URL
    const v = (await listVideos()).find((x) => x.video_id === id);
    td.innerHTML = v ? videoStatus(v, state.job) : "uploaded";
  } catch (e) { td.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
  input.value = "";
}

// Upload a video to this machine and put its file:// URL in the form's Video URL cell.
async function uploadVideoFile(input) {
  const f = input.files[0];
  if (!f) return;
  const row = +input.dataset.row, file = input.dataset.file;
  const field = main.querySelector(`.fc[data-file="${CSS.escape(file)}"][data-row="${row}"][data-col="video_url"]`);
  const label = input.closest("label"), was = label.firstChild.textContent;
  label.firstChild.textContent = "Uploading…";
  const body = new FormData();
  body.append("file", f);
  try {
    const { url } = await api("/api/video-files", { method: "POST", body });
    form.data[file][row].video_url = url;
    if (field) { field.value = url; field.classList.remove("invalid"); field.removeAttribute("title"); }
    saveDraft();
  } catch (e) {
    if (field) { field.classList.add("invalid"); field.title = `Upload failed: ${e.message}`; }
  }
  label.firstChild.textContent = was;
  input.value = "";
}

async function uploadAnswerKeys() {
  const files = $("#ak-files").files, msg = $("#ak-msg");
  if (!files.length) { msg.textContent = "Choose ground_truth_media.csv first."; return; }
  const body = new FormData();
  for (const f of files) body.append("files", f);
  try {
    await api("/api/dataset/answer-keys", { method: "POST", body });
    renderResults();
  } catch (e) { msg.textContent = `Not added: ${e.message}`; }
}

// ------------------------------------------------------------------ card pieces
function scoreBlock(s) {
  if (!s) return "";
  const has = s.score != null;
  return `
    <div class="score-row">
      <div class="score-num">${has ? s.score : "—"}<small> / 100</small></div>
      <div>${chip(s.verdict)}<div style="font-size:13px;margin-top:4px">Confidence: <b>${esc(s.confidence || "n/a")}</b></div></div>
      <button class="btn ghost small" id="tts" style="margin-left:auto">🔊 Listen to summary</button>
    </div>
    ${has ? `
    <div class="range" role="img" aria-label="Score ${s.score}, plausible range ${s.score_low} to ${s.score_high}">
      <div class="band" style="left:${s.score_low}%;width:${Math.max(1, s.score_high - s.score_low)}%"></div>
      <div class="mark" style="left:${s.score}%"></div>
    </div>
    <div class="range-labels"><span>0</span>
      <span>plausible range <b>${s.score_low}–${s.score_high}</b> (${esc(s.range_source)})</span><span>100</span></div>` : ""}
    <ol class="reasons">${s.reasons.map((r) => `<li>${linkTs(esc(r))}</li>`).join("")}</ol>
    ${s.usage?.model ? `<p class="hint">Scored by ${esc(s.usage.provider || "anthropic")} / ${esc(s.usage.model)}${s.prompt_id ? ` · prompt ${esc(s.prompt_id)}` : ""}</p>` : ""}`;
}

function banners(c) {
  const s = c.score || {};
  let h = "";
  if (s.rule_override) {
    h += `<div class="banner warn"><b>Rule guard overrode the model.</b> The model said ${esc(label(s.model_verdict))};
      the final verdict is DEFER TO HUMAN because: ${esc(s.rule_hits.join("; "))}.</div>`;
  } else if (s.rule_hits?.length) {
    h += `<div class="banner warn"><b>Data rule applies:</b> ${esc(s.rule_hits.join("; "))}.</div>`;
  }
  if (c.injection_flags.length) {
    h += `<div class="banner danger"><b>Injection guard: ${c.injection_flags.length} suspected instruction(s) in third-party text.</b>
      Treated as data, not obeyed.<ul>${c.injection_flags.map((f) =>
        `<li>${esc(f.channel.replaceAll("_", " "))}${f.t_s != null ? ` at ${linkTs(esc(fmtTs(f.t_s)))}` : ""}:
         <code>${esc(f.text)}</code></li>`).join("")}</ul></div>`;
  }
  return h;
}

function strip(frames) {
  if (!frames.length) return `<p>Keyframes: not available</p>`;
  return `<div class="strip">${frames.map((f) => `
    <button class="kf ${f.flagged ? "flagged" : ""}" data-t="${f.t_s}" title="Jump to ${fmtTs(f.t_s)}">
      ${f.url ? `<img loading="lazy" src="${esc(f.url)}" alt="Keyframe at ${fmtTs(f.t_s)}">` : `<div class="ph"></div>`}
      <div class="body">
        <div class="t"><span>${fmtTs(f.t_s)}</span>${f.flagged ? '<span class="chip DO_NOT_SCALE">flagged</span>' : ""}</div>
        <div>${esc(f.note || "not available")}</div>
        <div class="ost">${f.on_screen_text ? `“${esc(f.on_screen_text)}”` : "<i>no on-screen text</i>"}</div>
      </div>
    </button>`).join("")}</div>`;
}

function features(f) {
  const na = "not available";
  const yn = (b) => (b == null ? na : b ? "yes" : "no");
  f = f || {};
  const product = f.product_visible == null ? na
    : (f.product_visible && f.product_first_seen_s != null ? fmtTs(f.product_first_seen_s) : "never shown");
  const issues = f.safety_issues == null ? na : !f.safety_issues.length ? "none found"
    : f.safety_issues.map((i) => `${label(i.type)}${i.t_s != null ? ` @ ${fmtTs(i.t_s)}` : ""}`).join(", ");
  const rows = [
    ["Hook (first 3s)", f.hook_strength || na], ["Product first seen", product],
    ["Matches brand", f.brand_match || na],
    ["Call to action", yn(f.cta_present) + (f.cta_present && f.cta_t_s != null ? ` @ ${fmtTs(f.cta_t_s)}` : "")],
    ["Face on screen", yn(f.face_on_screen)], ["Safety problems", issues],
  ];
  return `<dl class="features">${rows.map(([k, v]) => `<dt>${k}</dt><dd>${linkTs(esc(v))}</dd>`).join("")}</dl>
    ${f.hook_reason ? `<p style="font-size:13px;margin:12px 0 0">${esc(f.hook_reason)}</p>` : ""}`;
}

function chart(m) {
  // Rates share one axis (they are all "% of reach"); skip rate is 0-100 on its own axis.
  const rates = [["Engagement rate", m.rates.engagement_rate], ["Save rate", m.rates.save_rate], ["Share rate", m.rates.share_rate]];
  const max = Math.max(0.01, ...rates.map(([, v]) => v || 0));
  const row = (name, width, text, alt) => `
    <div class="bar-row"><span>${name}</span>
      <div class="track">${width == null ? "" : `<div class="fill ${alt ? "alt" : ""}" style="width:${Math.min(100, width)}%"></div>`}</div>
      <span class="v ${text == null ? "na" : ""}">${text ?? "not available"}</span></div>`;
  const r = m.raw;
  return `<div class="bars">
      ${rates.map(([n, v]) => row(n, v == null ? null : (v / max) * 100, pct(v))).join("")}
      ${row("Skip rate", m.skip_rate_pct, m.skip_rate_pct == null ? null : `${m.skip_rate_pct.toFixed(1)}%`, true)}
    </div>
    <div class="counts">
      ${[["Reach", r.reach], ["Views", r.views], ["Likes", r.likes], ["Comments", r.comments], ["Shares", r.shares], ["Saved", r.saved]]
        .map(([k, v]) => `<div><b class="${v == null ? "na" : ""}">${int(v)}</b>${k}</div>`).join("")}
      <div><b>${m.avg_watch_time_s == null ? "not available" : m.avg_watch_time_s.toFixed(1) + "s"}</b>Avg watch</div>
    </div>`;
}

function decisionBar() {
  return `<section class="panel decision" aria-label="Human decision">
    <button class="btn" data-action="scale">Scale</button>
    <button class="btn danger" data-action="do_not_scale">Don't scale</button>
    <button class="btn ghost" id="ov-toggle">Override…</button>
    <span class="timer">Deciding for <b id="elapsed">0s</b> · ${mode() === "card" ? "card view" : "text-only view"}</span>
    <div class="override" id="ov">
      <select id="ov-verdict" aria-label="Override verdict">
        <option value="DEFER_TO_HUMAN">Defer / escalate</option>
        <option value="FLAG_AUTHENTICITY">Flag authenticity</option>
        <option value="SCALE">Scale</option>
        <option value="DO_NOT_SCALE">Do not scale</option>
      </select>
      <input id="ov-note" placeholder="Why are you overriding? (required)">
      <button class="btn" data-action="override">Log override</button>
    </div>
    <div class="logged" id="logged"></div>
  </section>`;
}

// ------------------------------------------------------------------ views
function renderCard(c) {
  const head = `<section class="panel head">
      <h1>${esc(c.brand)} <span style="font-weight:400">· @${esc(c.creator)}</span></h1>
      <div class="meta">${esc(c.video_id)} · ${esc(c.brand_category)} · posted ${c.hours_since_post ?? "?"}h ago ·
        spoken language: ${esc(c.language || "not available")} · media: ${esc(c.media_source || "not available")}</div>
      <div class="cap">${esc(c.caption)}</div>${c.prompt_id !== c.active_prompt
        ? `<p class="hint">This session's run used <b>${esc(c.prompt_id)}</b>; the active (adopted) prompt is <b>${esc(c.active_prompt)}</b>.</p>` : ""}${mode() === "card" ? banners(c) : ""}</section>`;

  if (!c.score) {
    main.innerHTML = head + `<section class="panel" style="text-align:center">
      <p>This video has not been through the pipeline yet.</p>
      <button class="btn" id="run">Run pipeline</button><p id="run-msg" style="font-size:13px"></p></section>`;
    return;
  }

  if (mode() === "text") {
    main.innerHTML = head + `
      <section class="panel"><h3>Data summary</h3><pre class="text">${esc(c.text_summary)}</pre></section>
      <section class="panel"><h3>AI recommendation</h3><pre class="text">${esc(c.score.raw_output)}</pre></section>`
      + decisionBar();
  } else {
    main.innerHTML = head + `
      <section class="panel top">
        <div>${c.video_url ? `<video controls playsinline preload="metadata" src="${esc(c.video_url)}">
            ${c.captions_url ? `<track kind="captions" srclang="${esc(c.language || "und")}" label="Transcript" src="${esc(c.captions_url)}" default>` : ""}
          </video>` : `<div class="no-video">No clip on disk.<br>Dataset fixtures used.</div>`}</div>
        <div>${scoreBlock(c.score)}</div>
      </section>
      <section class="panel"><h3>Keyframes · AI notes</h3>${strip(c.frames)}</section>
      <div class="grid2">
        <section class="panel"><h3>Engagement</h3>${chart(c.metrics)}</section>
        <section class="panel"><h3>Content features</h3>${features(c.features)}</section>
      </div>
      <section class="panel"><h3>Transcript</h3>${
        c.transcript == null ? "<p>not available</p>" : !c.transcript.length ? "<p>No speech detected.</p>"
        : `<ul class="transcript">${c.transcript.map((s) =>
            `<li>${linkTs(esc(fmtTs(s.start_s)))} ${esc(s.text)}</li>`).join("")}</ul>`}</section>
      <details class="panel"><summary>What the model read (flatten, 7 sections)</summary>
        <pre class="text" id="mm-summary">loading…</pre></details>`
      + decisionBar();
    api(`/api/videos/${c.video_id}/summary?multimodal=true`).then((t) => { $("#mm-summary").textContent = t; });
    const v = $("video");
    if (v) v.addEventListener("loadedmetadata", () => { for (const t of v.textTracks) t.mode = "showing"; });
  }
  startTimer();
}

function startTimer() {
  clearInterval(state.timerId);
  state.startedAt = performance.now();
  state.timerId = setInterval(() => {
    const el = $("#elapsed");
    if (el) el.textContent = `${Math.round((performance.now() - state.startedAt) / 1000)}s`;
  }, 500);
}

async function openVideo(id) {
  speechSynthesis.cancel();
  state.videoId = id;
  main.innerHTML = `<p class="empty">Loading…</p>`;
  try {
    state.card = await api(`/api/videos/${id}/card`);
    renderCard(state.card);
  } catch (e) { main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; }
  loadList();
}

async function decide(action) {
  const body = { action, reviewer: $("#reviewer").value, mode: mode(),
    elapsed_ms: Math.round(performance.now() - state.startedAt), note: "" };
  if (action === "override") {
    body.override_verdict = $("#ov-verdict").value;
    body.note = $("#ov-note").value;
    if (!body.note.trim()) { $("#logged").textContent = "An override needs a note."; return; }
  }
  try {
    const r = await api(`/api/videos/${state.videoId}/decision`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    clearInterval(state.timerId);
    $("#logged").textContent = `Logged ${r.ts}: ${label(r.human_verdict)} in ${(r.elapsed_ms / 1000).toFixed(1)}s — ` +
      (r.agrees_with_ai ? "agrees with the AI." : `differs from the AI (${label(r.ai_verdict)}).`);
    main.querySelectorAll(".decision button").forEach((b) => (b.disabled = true));
    loadList();
  } catch (e) { $("#logged").textContent = `Not logged: ${e.message}`; }
}

// Evaluation has three parts: the blind manual review (human baseline), the locked ground truth
// (real results), and the results measured against it.
async function renderEval(runMissing = false) {
  state.evalTab ||= "manual";
  const tabs = { manual: "Manual review", reviews: "Reviews", truth: "Ground truth", results: "Results", prompts: "Prompts for this campaign" };
  main.innerHTML = `<nav class="subtabs" role="tablist">
      ${Object.entries(tabs).map(([k, v]) => `<button class="tab ${state.evalTab === k ? "active" : ""}" data-evaltab="${k}" role="tab">${v}</button>`).join("")}
    </nav><div id="eval-body"><p class="empty">Loading…</p></div>`;
  return { manual: renderManual, reviews: renderReviews, truth: renderTruth, results: renderResults,
    prompts: renderPrompts }[state.evalTab](runMissing);
}

// Which reviewers count as "Human" in Results: everyone (state.rvFilter null) or a chosen set.
function reviewerFilter(r) {
  if (!r.all.length) return "";
  const everyone = !state.rvFilter;
  return `<section class="panel rvfilter"><h3>Reviewers</h3>
    <div class="row" role="group" aria-label="Reviewers counted as Human">
      <button class="btn small ${everyone ? "" : "ghost"}" data-rvall aria-pressed="${everyone}">Everyone (${r.all.length})</button>
      ${r.all.map((n) => `<label class="rvpick"><input type="checkbox" data-rvpick="${esc(n)}"
        ${!everyone && r.selected.includes(n) ? "checked" : ""}> ${esc(n)}</label>`).join("")}</div>
    <p class="hint" style="margin:6px 0 0">${everyone ? "Every submitted reviewer counts as Human below. Tick names to compare only some."
      : `Human below = ${r.selected.map(esc).join(", ")}. The AI rows do not change.`}</p></section>`;
}

// The rule guard thresholds: editable per campaign, the config defaults until changed.
function thresholdPanel(th) {
  if (!th) return "";
  const v = th.values, d = th.defaults;
  const changed = Object.keys(d).some((k) => v[k] !== d[k]);
  const field = (k, name, unit) => `<label class="th-field">${name}
    <input type="number" min="0" step="any" id="th-${k}" value="${v[k]}" placeholder="${d[k]}">
    <span class="hint">${unit} · default ${d[k].toLocaleString()}</span></label>`;
  return `<section class="panel thresholds"><h3>Rule guard thresholds</h3>
    <div class="row">${field("min_hours", "Minimum hours since post", "hours")}${field("min_reach", "Minimum reach", "accounts")}
      <button class="btn small" id="th-save">Save</button>
      ${changed ? `<button class="btn ghost small" id="th-reset">Back to defaults</button>` : ""}</div>
    <p class="hint" style="margin:6px 0 0">A video under either one ends as defer to human, whatever the model said.
      Changing them re-applies the rules to the scores already made (no new model call), so the AI rows below update.
      ${changed ? "<b>Changed from the defaults for this campaign.</b>" : "Using the defaults."}</p>
    <p id="th-msg" class="msg"></p></section>`;
}

async function saveThresholds(reset) {
  const body = reset ? {} : { min_hours: $("#th-min_hours").value, min_reach: $("#th-min_reach").value };
  try { await api("/api/thresholds", { method: "PUT", body: JSON.stringify(body), headers: { "Content-Type": "application/json" } }); }
  catch (err) { $("#th-msg").textContent = err.message; return; }
  renderResults();
}

// The version whose answers fill the "P6" rows of the open evaluation (P6, P6.2, ...); keys stay "P6 ..." in the API.
let evalP = "P6";
const pname = (s) => String(s).replace(/\bP6(?![\w.])/g, evalP);

// A result row or column named after a prompt (P5 / P6) shows only once that prompt has scored something.
const shown = (name, has) => !/^P5/.test(name) || has.p5 ? !/^P6/.test(name) || has.p6 : false;

// Precision / recall / F1 / accuracy of the three methods; recall leads, since a missed winner costs most.
function classificationPanel(c, has) {
  const pc = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  const basis = evalBasis(c);
  const names = { [basis]: `Measured against the ${evalBasisName[basis]}` };
  const scopes = { all: "Every video each method decided", same: "Only the videos the reviewers decided (same items for all three)" };
  const table = (m) => `<table class="cls"><tr><th>Method</th><th>Recall</th><th>Precision</th><th>F1</th>
      <th title="Misleading when winners are rare">Accuracy ⚠</th><th title="true pos · false pos · missed winners · true neg">TP · FP · FN · TN</th><th>n</th></tr>
    ${Object.entries(scopes).map(([s, title]) => `<tr><td colspan="7" class="cls-scope">${title}</td></tr>
      ${Object.entries(m[s]).filter(([name]) => shown(name, has)).map(([name, x]) => `<tr><td>${esc(pname(name))}</td>
        <td class="num"><b>${pc(x.recall)}</b>${x.winners ? ` <span class="hint">${x.tp}/${x.winners}</span>` : ""}</td>
        <td class="num">${pc(x.precision)}${x.tp + x.fp ? ` <span class="hint">${x.tp}/${x.tp + x.fp}</span>` : ""}</td>
        <td class="num">${pc(x.f1)}</td><td class="num">${pc(x.accuracy)}</td>
        <td class="num">${x.n ? `${x.tp} · ${x.fp} · ${x.fn} · ${x.tn}` : "—"}</td><td class="num">${x.n}</td></tr>`).join("")}`).join("")}</table>`;
  return `<section class="panel"><h3>Precision, recall, F1</h3>
    <p style="font-size:13px">"Scale" is the positive class: a <b>winner</b> is a video the truth says to scale.
      An AI verdict other than SCALE (defer, flag) counts as don't scale, as for the reviewers.${has.p5 || has.p6
        ? ` ${[has.p5 && "P5", has.p6 && evalP].filter(Boolean).join(" and ")} ${has.p5 && has.p6 ? "are their" : "is its"} final
        verdict${has.p5 && has.p6 ? "s" : ""}, after the rule guard.` : ""}</p>
    ${Object.entries(names).map(([k, title]) => {
      const m = c[k];
      if (!m.videos) return `<h4>${title}</h4><p class="hint">Lock the ground truth first. (No video has a real CAC outcome yet either.)</p>`;
      return `<h4>${title}</h4>${table(m)}
        <p class="hint">${m.winners} winner${m.winners === 1 ? "" : "s"} among ${m.videos} video${m.videos === 1 ? "" : "s"} with a known result.
          ⚠ Accuracy: a method that never scales anything would score <b>${pc(m.never_scale_accuracy)}</b> here while catching no winner,
          so read it next to recall.</p>`;
    }).join("")}
    <p style="font-size:13px"><b>Recall</b>: of all the real winners, how many were caught (the main metric: a missed winner hurts most).
      <b>Precision</b>: of everything scaled, how much was actually good (the cost: wasted ad money).
      <b>F1</b>: balances the two in one number. <b>Accuracy</b>: of all decisions, how many were right.
      Human rows pool every reviewer's decisions.</p></section>`;
}

const STAGE_NAMES = { download: "Download video", preprocess: "Keyframes + transcript", analyse: "Visual analysis",
  score_p6: "P6 multimodal score", score_p5: "P5 text-only score (evaluation only)" };

function timingPanel(pt) {
  const e2e = pt.end_to_end, h = pt.human;
  const faster = e2e.mean_s && h.mean_s ? h.mean_s / e2e.mean_s : null;
  return `<section class="panel"><h3>Pipeline time</h3>
    <div class="stat"><div><b>${dur(e2e.mean_s)}</b> per video, AI pipeline<br><span style="font-size:13px">mean of ${e2e.n} timed
        video${e2e.n === 1 ? "" : "s"} · median ${dur(e2e.median_s)} · ${dur(e2e.total_s)} in all</span></div>
      <div><b>${dur(h.mean_s)}</b> per video, blind human review<br><span style="font-size:13px">${h.n
        ? `mean of ${h.n} decision${h.n === 1 ? "" : "s"} · median ${dur(h.median_s)}` : "no submitted review with minutes yet"}</span></div>
      ${faster ? `<div><b>${faster >= 1 ? `${faster.toFixed(1)}× faster` : `${(1 / faster).toFixed(1)}× slower`}</b><br>
        <span style="font-size:13px">AI vs human, mean per video</span></div>` : ""}</div>
    <table><tr><th>Stage</th><th>Videos timed</th><th>Mean</th><th>Median</th><th>Total</th></tr>
      ${Object.entries(pt.stages).filter(([, s]) => s.n).map(([k, s]) => `<tr><td>${esc(pname(STAGE_NAMES[k] || k))}</td><td class="num">${s.n}</td>
        <td class="num">${dur(s.mean_s)}</td><td class="num">${dur(s.median_s)}</td><td class="num">${dur(s.total_s)}</td></tr>`).join("")}
      ${e2e.n ? `<tr><td><b>End to end</b> <span class="hint">(download + keyframes + vision + ${esc(evalP)})</span></td><td class="num">${e2e.n}</td>
        <td class="num"><b>${dur(e2e.mean_s)}</b></td><td class="num">${dur(e2e.median_s)}</td><td class="num">${dur(e2e.total_s)}</td></tr>` : ""}</table>
    ${Object.keys(pt.per_video).length ? `<p class="hint">Per video: ${Object.entries(pt.per_video).map(([v, s]) =>
      `${esc(v)} ${dur(s)}`).join(" · ")}</p>` : ""}
    <p style="font-size:13px">Wall-clock seconds each step took the last time it actually ran for a video (cached steps
      keep their earlier time); downloads count only for videos fetched from a URL. Videos processed before timing was added
      show no time until they are re-run (Regenerate dataset, or upload a new clip). Human time is the blind reviewers'
      minutes spent per video.</p></section>`;
}

// The evaluation of every child session of a parent, and the mean across them.
function collectionDashboard(r) {
  const pc = (x) => (x == null ? "—" : `${Math.round(x * 100)}%`);
  const aggRows = Object.entries(r.aggregate).map(([name, m]) => `<tr><td>${esc(name)}</td>
    ${["cac_outcome"].map((k) => `<td class="num">${m[k].runs ? `<b>${pc(m[k].mean_accuracy)}</b>
      <span class="hint">${pc(m[k].min_accuracy)}–${pc(m[k].max_accuracy)} · ${plural(m[k].runs, "run")}</span>` : "—"}</td>
      <td class="num">${pc(m[k].mean_recall)}</td>`).join("")}</tr>`).join("");
  const cellAcc = (a) => (a && a.n ? `${pc(a.accuracy)} <span class="hint">${a.correct}/${a.n}</span>` : "—");
  const names = Object.keys(r.aggregate);
  return `<h4>${esc(r.name)}: ${r.evaluated} of ${r.runs.length} runs evaluated${r.failed ? `, ${r.failed} failed` : ""}</h4>
    ${r.runs.length ? `${aggRows ? `<table><tr><th rowspan="2">Prompt (mean over runs)</th><th colspan="2">vs CAC outcome</th></tr><tr><th>Accuracy</th><th>Recall</th></tr>${aggRows}</table>` : ""}
      <table><tr><th>Run</th><th>Prompt · model</th><th>Scored</th>${names.map((n) => `<th>${esc(n)}</th>`).join("")}<th></th></tr>
      ${r.runs.map((x) => `<tr><td>${esc(x.name ?? x.id)}</td><td>${x.run ? `<b>${esc(x.run.prompt_id)}</b> · ${esc(x.run.provider)} / ${esc(x.run.model)}${x.run.status && x.run.status !== "done" ? ` <span class="hint">${esc(x.run.status)}</span>` : ""}` : esc(x.llm.model ?? x.llm.provider)}</td>
        ${x.error ? `<td colspan="${names.length + 1}" class="bad">${esc(x.error)}</td>`
          : `<td class="num">${x.scored}/${x.videos}</td>${names.map((n) => `<td class="num">${cellAcc(x.verdict_accuracy[n]?.cac)}</td>`).join("")}`}
        <td><button class="btn ghost small" data-open-session="${esc(x.id)}" data-then="eval">Open</button></td></tr>`).join("")}</table>
      <p class="hint">Accuracy against the real CAC outcome (CAC vs target CAC); a blank means that run has not been scored. Averages skip blanks, never count them as 0.</p>`
      : `<p class="hint">This collection has no runs.</p>`}`;
}

// Every run's submitted reviews per video; identical calls from all reviewers show once.
function collectionReviews(rv) {
  const labels = rv.columns;                       // the server orders them (runs numerically)
  const one = (c) => `<span class="chip ${esc(c.decision)}">${esc(CALL[c.decision] || label(c.decision))}</span>
    ${c.reason ? `<div class="cap rv-reason">${esc(c.reason)}</div>` : ""}`;
  const runsOf = (c) => (c.runs?.length > 1 ? `<div class="cap">${c.count}× · runs ${c.runs.map(esc).join(", ")}</div>`
    : c.runs?.length ? `<div class="cap">run ${esc(c.runs[0])}</div>` : "");
  // unique mode: one cell per reviewer, their distinct calls stacked, each with the runs it came from
  const cell = (c) => !c ? '<span class="na">—</span>'
    : c.variants ? c.variants.map((x) => `<div>${one(x)}${runsOf(x)}</div>`).join("") : one(c);
  const errs = rv.runs.filter((x) => x.error);
  return `<section class="panel"><h3>Reviews · all runs of ${esc(rv.name)}</h3>
    <label class="hint"><input type="checkbox" id="coll-unique" ${rv.unique ? "checked" : ""}> Unique reviews only
      (the same reviewer's identical call, repeated across runs, shows once)</label>
    ${errs.map((x) => `<p class="bad">${esc(x.name ?? x.id)}: ${esc(x.error)}</p>`).join("")}
    ${labels.length ? `<div class="fwrap"><table class="rv rv-grid"><tr><th>Video</th>${labels.map((l) => `<th>${esc(l)}</th>`).join("")}<th>Agreement</th></tr>
      ${rv.videos.map((v) => `<tr><td>${esc(v.video_id)}<div class="cap">@${esc(v.creator || "—")} · ${esc((v.caption || "").slice(0, 60))}</div></td>
        ${v.merged ? `<td colspan="${labels.length}">${cell(v.merged)}
            <div class="cap">same call from ${v.merged.count} reviews: ${v.merged.reviewers.map(esc).join(", ")}</div></td>`
          : labels.map((l) => `<td>${cell(v.calls[l])}</td>`).join("")}
        <td>${v.agreement === "agree" ? '<span class="chip SCALE">Agree</span>' : v.agreement === "split" ? '<span class="chip DEFER_TO_HUMAN">Split</span>' : '<span class="na">—</span>'}</td></tr>`).join("")}
    </table></div><p class="hint">Submitted reviews only; drafts stay hidden.${rv.agreement.n ? ` Agree on ${rv.agreement.agree} of ${rv.agreement.n} videos with 2 or more reviews.` : ""}</p>`
      : "<p>No submitted review in any run yet.</p>"}</section>`;
}

async function renderResults(runMissing = false) {
  const body = $("#eval-body");
  body.innerHTML = `<p class="empty">${runMissing ? "Running pipeline on unscored videos…" : "Loading…"}</p>`;
  let e;
  const who = state.rvFilter ? [...state.rvFilter].map((n) => `&reviewer=${encodeURIComponent(n)}`).join("") : "";
  try { e = await api(`/api/evaluate?run_missing=${runMissing}${who}`); }       // the session's own run prompt (server default)
  catch (err) { body.innerHTML = `<p class="empty">Error: ${esc(err.message)}</p>`; return; }
  evalP = e.prompt || "P6";
  const ds = await api("/api/dataset").catch(() => ({ id: null, files: [] }));
  const th = ds.id == null ? null : await api("/api/thresholds").catch(() => null);
  const has = { p5: e.videos.some((v) => v.p5_model || v.p5_final), p6: e.videos.some((v) => v.p6_model || v.p6_final) };
  const gtLocked = ds.files.includes("ground_truth.csv"), media = ds.files.includes("ground_truth_media.csv");
  const keyPanel = `<section class="panel"><h3>Answer keys</h3>
    ${ds.id == null ? "<p>Open a campaign in the Dataset tab first.</p>" : `
    <p>Ground truth: ${gtLocked ? "<b>locked</b>, results below are measured against it"
      : 'not locked yet. Fill in and lock it under <button class="linkish" data-evaltab="truth">Ground truth</button>.'}<br>
      Expected vision features (<code>ground_truth_media.csv</code>, optional): ${media ? "loaded" : "not loaded"}.
      Both are only read here, never by the scoring steps.</p>
    <div class="row"><input type="file" id="ak-files" accept=".csv,text/csv" aria-label="ground_truth_media.csv">
      <button class="btn ghost small" id="ak-upload">Add ground_truth_media.csv</button></div>
    <p id="ak-msg" class="msg"></p>`}</section>`;
  const sid = ds.id == null ? null : encodeURIComponent(ds.id);
  const exportPanel = sid == null ? "" : `<section class="panel"><h3>Export</h3>
    <p class="hint" style="margin-top:0">Hand the prompt pack to another AI to improve P6: it holds the brief and constraints,
      the current P6, this evaluation, and per video the exact input, the model's answer, human decisions
      and the real outcome (misses first).${gtLocked ? "" : " <b>Lock ground truth first</b>, or the pack has no outcomes to learn from."}</p>
    <div class="row">
      <a class="btn small" href="/api/sessions/${sid}/prompt-pack" download>Download prompt pack for AI (.md)</a>
      <a class="btn ghost small" href="/api/sessions/${sid}/export" download>Download results (.zip)</a>
      <a class="btn ghost small" href="/api/sessions/${sid}/export?media=true" download title="Adds clips, audio, keyframes and recap videos (can be large)">Results + media (.zip)</a>
    </div></section>`;
  const cell = (got, exp) => `<td class="${got == null ? "" : got === exp ? "ok" : "bad"}">${esc(label(got))}</td>`;
  const t = e.reviewer_timing;
  const accCell = (a) => `<td class="num">${a.n ? `${a.correct} / ${a.n} · ${(a.accuracy * 100).toFixed(0)}%` : "—"}</td>`;
  const basis = evalBasis(e.classification);
  const basisCap = evalBasisName[basis][0].toUpperCase() + evalBasisName[basis].slice(1);
  const manualPanel = `<section class="panel"><h3>Manual review vs AI</h3>
    ${e.manual_review.length ? `<table><tr><th>Reviewer</th><th>Videos</th><th>Measured against</th><th>Human (blind)</th>
        ${has.p5 ? "<th>P5 final</th>" : ""}${has.p6 ? `<th>${esc(evalP)} final</th>` : ""}<th>Minutes / video</th></tr>
      ${e.manual_review.map((m) => `<tr>
        <td><b>${esc(m.reviewer)}</b></td><td class="num">${m.videos}</td>
        <td>${esc(basisCap)}</td>${accCell(m[basis].human)}${has.p5 ? accCell(m[basis].p5_final) : ""}${has.p6 ? accCell(m[basis].p6_final) : ""}
        <td class="num">${m.mean_minutes ?? "—"}${m.total_minutes != null ? `<br><span class="hint">${m.total_minutes} total</span>` : ""}</td></tr>`).join("")}</table>
      <p style="font-size:13px">Scored on exactly the videos each reviewer decided, so human and AI are judged on the same set.
        Reviewers choose only scale / don't scale, so every AI verdict other than SCALE (defer, flag) counts as don't scale.
        ${basis === "cac_outcome" ? "<b>Real CAC outcome</b>: scale when the video performed well after scaling (CAC ≤ target), don't scale when it did not; videos never scaled are left out."
          : "<b>Expected verdict</b>: the ground truth's verdict (no video has a real CAC outcome yet)."}</p>`
    : `<p>No submitted manual review yet. Fill one in under <button class="linkish" data-evaltab="manual">Manual review</button>.</p>`}</section>`;
  const accRows = Object.entries(e.verdict_accuracy).filter(([k]) => shown(k, has));
  const pi = e.prompt_info || {};
  const methodsPanel = `<section class="panel"><h3>Methods evaluated</h3>
    <p style="font-size:13px;margin:0"><b>Human (blind)</b> · <b>P5 text-only</b> · <b>${esc(evalP)} multimodal</b>
      <span class="hint">on ${esc(e.llm?.provider || "")} / ${esc(e.llm?.model || "no model")}</span><br>
      <span class="hint">${esc(evalP)}: ${pi.scored ?? "?"} of ${pi.videos ?? "?"} videos scored${pi.other_text ? `, ${pi.other_text} from a different text under this id (re-score)` : ""}${
        pi.active && pi.active !== evalP ? ` · this session's run used ${esc(evalP)}; the active (adopted) prompt is ${esc(pi.active)}` : ""}.</span></p></section>`;
  body.innerHTML = methodsPanel + exportPanel + reviewerFilter(e.reviewers) + thresholdPanel(th) + keyPanel + classificationPanel(e.classification, has) + manualPanel + `
    <section class="panel"><h3>Verdict accuracy vs ground truth</h3>
      ${accRows.length ? `<table><tr><th rowspan="2">Prompt</th><th colspan="3">Measured against the ${evalBasisName[basis]}</th></tr>
        <tr><th>Recall</th><th>Correct</th><th>Accuracy</th></tr>
      ${accRows.map(([k, v]) => `<tr><td>${esc(pname(k))}</td>${[basis === "cac_outcome" ? v.cac || { n: 0 } : v].map((a) => `<td class="num"><b>${a.recall == null ? "—"
          : (a.recall * 100).toFixed(0) + "%"}</b>${a.winners ? ` <span class="hint">${a.tp}/${a.winners}</span>` : ""}</td>
        <td class="num">${a.n ? `${a.correct} / ${a.n}` : "—"}</td>
        <td class="num">${a.accuracy == null ? "—" : (a.accuracy * 100).toFixed(0) + "%"}</td>`).join("")}</tr>`).join("")}</table>
      <p style="font-size:13px"><b>Recall</b>: of the real winners (truth says scale), how many the prompt scaled.
        A verdict of defer or flag on a winner counts as missed.</p>
      <p style="font-size:13px">${basis === "cac_outcome"
        ? `<b>Real CAC outcome</b>: right when the verdict matches what the real CAC showed after scaling
        (CAC ≤ target = scale). Only scale / don't scale is compared, so defer and flag count as don't scale; videos never
        scaled have no CAC outcome and are left out.`
        : `<b>Expected verdict</b>: used because no video has a real CAC outcome yet; once one does, this table switches to it.`}</p>
      <p style="font-size:13px">${[has.p5 && "P5 is compared with <code>ground_truth.csv</code>",
        has.p6 && `${esc(evalP)} with the multimodal key in <code>ground_truth_media.csv</code>`].filter(Boolean).join("; ")}.
        "After rule guard" applies the ${th ? `${th.values.min_hours}h / ${th.values.min_reach.toLocaleString()} reach` : "hours / reach"} / missing-metric rules in code.</p>`
      : "<p>No video has been scored yet.</p>"}
      <button class="btn ghost small" id="eval-run">Score any unprocessed videos</button></section>
    <section class="panel"><h3>Per video</h3>
      <table><tr><th>Video</th><th>Scenario</th><th>Expected (text / multimodal)</th>
        ${has.p5 ? "<th>P5 model</th><th>P5 final</th>" : ""}
        ${has.p6 ? `<th>${esc(evalP)} model</th><th>${esc(evalP)} final</th><th>${esc(evalP)} reasons citing time</th>` : ""}<th>Human (blind)</th></tr>
      ${e.videos.map((v) => `<tr><td>${esc(v.video_id)}</td><td>${esc(label(v.scenario))}</td>
        <td>${esc(label(v.expected))} / ${esc(label(v.expected_multimodal))}</td>
        ${has.p5 ? cell(v.p5_model, v.expected) + cell(v.p5_final, v.expected) : ""}
        ${has.p6 ? `${cell(v.p6_model, v.expected_multimodal)}${cell(v.p6_final, v.expected_multimodal)}
        <td class="num">${esc(v.p6_reasons_citing_time ?? "—")}</td>` : ""}
        <td>${Object.entries(v.manual).map(([who, d]) => `<span class="${v.expected_scale == null ? "" : d === v.expected_scale ? "ok-text" : "bad-text"}"
          title="${esc(who)}">${esc(label(d))}</span>`).join("<br>") || "—"}</td></tr>`).join("")}</table></section>
    <section class="panel"><h3>Content features vs ground truth</h3>
      <table><tr><th>Feature</th><th>Correct</th><th>Misses (got → expected)</th></tr>
      ${Object.entries(e.feature_accuracy).map(([k, v]) => `<tr><td>${esc(label(k))}</td><td class="num">${v.correct} / ${v.n}</td>
        <td>${e.videos.filter((x) => x.features[k] && !x.features[k].ok).map((x) =>
          `${esc(x.video_id)}: ${esc(x.features[k].got ?? "none")} → ${esc(x.features[k].expected ?? "none")}`).join("; ") || "—"}</td></tr>`).join("")}</table></section>
    ${timingPanel(e.pipeline_timing)}
    <section class="panel"><h3>Reviewer decision time</h3>
      <div class="stat">${["card", "text"].map((m) => `<div><b>${t[m].median_s == null ? "—" : t[m].median_s + "s"}</b>
        median, ${m === "card" ? "with the card" : "text only"}<br><span style="font-size:13px">${t[m].decisions} decisions ·
        mean ${t[m].mean_s ?? "—"}s · agreed with AI ${t[m].agreed_with_ai}/${t[m].decisions}</span></div>`).join("")}</div>
      <p style="font-size:13px">Timed from the moment a video's view is shown until a decision is logged.
        Switch the mode toggle in the header to collect both conditions.</p></section>`;
}

// ------------------------------------------------------------------ ground truth (real results, locked)
function gtCell(i, c, v, locked) {
  v = v ?? "";
  if (locked) return `<span class="${v ? "" : "na"}">${esc(label(v) || "—")}</span>`;
  const err = state.gtErrors.find((e) => e.row === i && e.column === c.name);
  const a = `class="fc gc k-${c.kind}${err ? " invalid" : ""}" data-gt="${i}" data-col="${esc(c.name)}"
    aria-label="${esc(c.label)}, ${esc(state.gt.rows[i].video_id)}"${err ? ` title="${esc(err.message)}" aria-invalid="true"` : ""}`;
  if (c.kind === "enum") {
    const auto = c.name === "performed_well_after_scaling" && state.gt.target_cac != null ? "auto from CAC" : "";
    return `<select ${a}><option value="">${auto}</option>${c.options.map((o) =>
      `<option ${o.toLowerCase() === String(v).toLowerCase() ? "selected" : ""} value="${esc(o)}">${esc(label(o))}</option>`).join("")}</select>`;
  }
  if (c.kind === "long") return `<textarea ${a} rows="1" placeholder="${esc(c.hint)}">${esc(v)}</textarea>`;
  const mode = c.kind === "int" ? ' inputmode="numeric"' : c.kind === "float" ? ' inputmode="decimal"' : "";
  if (c.name === "video_url") return `<div class="url-cell"><input type="text" ${a} value="${esc(v)}"
      placeholder="${esc(c.hint.replace(/^optional: /, ""))}">
    <label class="btn ghost small" title="Upload a video from this computer; its file:// URL fills the field">Upload…
      <input type="file" class="url-file" data-file="${esc(file)}" data-row="${i}" accept="video/*" hidden
        aria-label="Upload video file, row ${i + 1}"></label></div>`;
  return `<input type="text" ${a}${mode} value="${esc(v)}" placeholder="${esc(c.hint)}"
    ${c.suggest ? `list="dl-gt-${esc(c.name)}"` : ""}>`;
}

async function renderTruth() {
  const body = $("#eval-body");
  let g, ds;
  if (state.gtDirty) flushTruth();          // a draft still being typed is saved before it is read back
  await state.gtSaving;
  try { [g, ds] = await Promise.all([api("/api/ground-truth"), api("/api/dataset")]); }
  catch (_) { body.innerHTML = `<section class="panel"><p>Open a campaign in the Dataset tab first.</p></section>`; return; }
  if (state.view !== "eval" || state.evalTab !== "truth") return;
  state.gt = g; state.gtErrors ||= [];
  const name = esc(ds.session?.name || "this campaign");
  if (!g.rows.length) {
    body.innerHTML = `<section class="panel"><h3>Ground truth · ${name}</h3>
      <p>Generate the campaign's dataset in the Dataset tab first: the results are entered per video.</p></section>`;
    return;
  }
  const locked = !!g.locked_at, cols = g.cols.filter((c) => !c.auto);
  body.innerHTML = `<section class="panel"><h3>Ground truth · ${name}</h3>
    ${locked ? `<div class="banner lock"><b>🔒 Locked ${esc(localTime(g.locked_at))}.</b> These real results can no longer change;
        every evaluation of this campaign is measured against them.<br>
        ${g.videos_at_lock} video${g.videos_at_lock === 1 ? "" : "s"} with results · SHA-256 <code>${esc(g.sha256.slice(0, 16))}…</code> ·
        copy saved as <code>${esc(g.file)}</code> ·
        ${g.scored_at_lock ? `<span class="warn-text">locked after ${g.scored_at_lock} of ${g.rows.length} videos were already scored</span>`
          : "locked before any video was scored"}</div>`
    : `<p>Enter what really happened to each video after the campaign: the right verdict, and the spend and
        conversions if it was scaled. Leave a video blank if its results are unknown; it is left out of the evaluation.
        ${g.target_cac != null ? `Target CAC for this campaign: <b>$${esc(g.target_cac)}</b>.` : "This campaign has no target CAC, so fill in <i>performed well</i> yourself."}
        Lock it <b>before</b> looking at the AI's scores, so the comparison is fair.</p>`}
    ${cols.filter((c) => c.suggest).map((c) => `<datalist id="dl-gt-${esc(c.name)}">
      ${c.suggest.map((o) => `<option value="${esc(o)}">`).join("")}</datalist>`).join("")}
    <div class="fwrap"><table class="form gt">
      <tr><th>Video</th>${cols.map((c) => `<th title="${esc(c.hint)}">${esc(c.label)}</th>`).join("")}</tr>
      ${g.rows.map((r, i) => `<tr><td><button class="vplay" data-gtplay="${i}" title="Watch this video"
          ${!r.url && !r.clip ? "disabled" : ""}>▶ ${esc(r.video_id)}</button><div class="cap">@${esc(r.creator || "—")} · ${esc((r.caption || "").slice(0, 60))}</div>
        <div class="vlinks">${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open post ↗</a>` : ""}
          ${!r.url && !r.clip ? '<span class="hint">no link or clip yet</span>' : ""}</div></td>
        ${cols.map((c) => `<td>${gtCell(i, c, r[c.name], locked)}</td>`).join("")}</tr>`).join("")}
    </table></div>
    ${state.gtErrors.length ? `<ul class="ferrs">${state.gtErrors.map((e) => `<li>${esc(e.message)}</li>`).join("")}</ul>` : ""}
    ${locked ? "" : `<div class="row" style="margin-top:12px">
        <label class="btn ghost small">Import from ground_truth.csv…
          <input type="file" id="gt-import" accept=".csv,text/csv" hidden></label>
        <span class="hint" id="gt-status">Saved as you type, as a draft in the campaign's session.json.</span></div>
      <div class="lockbox">
        <label><input type="checkbox" id="gt-confirm"> I understand that once locked, these results can never be changed.</label>
        <button class="btn" id="gt-lock" disabled>🔒 Lock ground truth</button>
        <p id="gt-msg" class="msg"></p></div>`}</section>`;
}

// Watch a video without leaving the Ground truth screen: its clip when there is one, else the
// post's own embed player (Instagram, TikTok, YouTube) or a direct video link.
function embedUrl(url) {
  let u;
  try { u = new URL(url); } catch (_) { return null; }
  const host = u.hostname.replace(/^www\./, ""), path = u.pathname;
  let m;
  if (host.endsWith("instagram.com") && (m = path.match(/^\/(?:[^/]+\/)?(p|reels?|tv)\/([A-Za-z0-9_-]+)/)))
    return `https://www.instagram.com/${m[1] === "tv" ? "tv" : m[1].startsWith("reel") ? "reel" : "p"}/${m[2]}/embed/`;
  if (host.endsWith("tiktok.com") && (m = path.match(/\/video\/(\d+)/))) return `https://www.tiktok.com/embed/v2/${m[1]}`;
  if (host === "youtu.be") return `https://www.youtube.com/embed/${path.slice(1)}`;
  if (host.endsWith("youtube.com")) {
    const id = u.searchParams.get("v") || (path.match(/^\/(?:shorts|embed)\/([\w-]+)/) || [])[1];
    return id ? `https://www.youtube.com/embed/${id}` : null;
  }
  return null;
}

function playVideo(r) {
  const embed = r.url && embedUrl(r.url);
  const player = r.clip ? `<video src="/api/videos/${encodeURIComponent(r.video_id)}/clip" controls autoplay playsinline></video>`
    : embed ? `<iframe src="${esc(embed)}" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen></iframe>`
    : r.url && /\.(mp4|mov|webm|m4v)(\?|$)/i.test(r.url) ? `<video src="${esc(r.url)}" controls autoplay playsinline></video>`
    : `<p class="hint">This link can't play here.</p>`;
  let dlg = $("#vdlg");
  if (!dlg) {
    dlg = document.createElement("dialog");
    dlg.id = "vdlg"; dlg.className = "vdlg";
    dlg.addEventListener("click", (ev) => { if (ev.target === dlg || ev.target.closest("[data-vclose]")) dlg.close(); });
    dlg.addEventListener("close", () => { dlg.innerHTML = ""; });   // stops playback
    document.body.append(dlg);
  }
  dlg.innerHTML = `<div class="vdlg-in"><div class="row"><b>${esc(r.video_id)}</b>
      <span class="hint">@${esc(r.creator || "—")}</span>
      ${r.url ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Open post ↗</a>` : ""}
      <button class="btn ghost small" data-vclose aria-label="Close video">✕</button></div>
    <div class="vbody"><div class="vframe${embed && !r.clip ? " embed" : ""}">${player}</div>
      <div class="vinfo">${r.caption ? `<h4>Caption</h4><p class="vcap">${esc(r.caption)}</p>` : ""}
        ${(r.info || []).map(([title, rows]) => `<h4>${esc(title)}</h4><dl>${rows.map(([k, v]) =>
          `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>`).join("")}</div></div></div>`;
  dlg.showModal();
}

function saveTruth() {
  clearTimeout(state.gtSaveId);
  if ($("#gt-status")) $("#gt-status").textContent = "Unsaved changes…";
  state.gtDirty = true;
  state.gtSaveId = setTimeout(flushTruth, 700);
}

// Only the result columns go back to the server; the rows also carry the viewer's info, link and clip.
const gtBody = () => JSON.stringify({ rows: state.gt.rows.map((r) => Object.fromEntries(
  [["video_id", r.video_id], ...state.gt.cols.filter((c) => !c.auto).map((c) => [c.name, r[c.name] ?? ""])])) });

function flushTruth() {
  clearTimeout(state.gtSaveId);
  state.gtDirty = false;
  state.gtSaving = (state.gtSaving || Promise.resolve()).then(async () => {
    try {
      await api("/api/ground-truth", { method: "PUT", headers: { "content-type": "application/json" },
        body: gtBody() });
      state.gtSaveError = null;
      if ($("#gt-status")) $("#gt-status").textContent = `Draft saved ${new Date().toLocaleTimeString()}.`;
    } catch (e) {
      state.gtSaveError = e.message;
      if ($("#gt-status")) $("#gt-status").textContent = `Not saved: ${e.message}`;
    }
  });
  return state.gtSaving;
}

async function lockTruth() {
  const btn = $("#gt-lock");
  btn.disabled = true;
  await flushTruth();
  try {
    await api("/api/ground-truth/lock", { method: "POST", headers: { "content-type": "application/json" },
      body: gtBody() });
    state.gtErrors = [];
    renderTruth();
  } catch (e) {
    state.gtErrors = e.detail?.errors || [];
    await renderTruth();
    if ($("#gt-msg")) $("#gt-msg").textContent = state.gtErrors.length
      ? `Not locked: ${state.gtErrors.length} problem(s), marked in red.` : `Not locked: ${e.message}`;
  }
}

async function importTruth(file) {
  const rows = parseCsv(await file.text()).map((r) => Object.fromEntries(
    Object.entries(r).map(([k, v]) => [k.trim().toLowerCase(), String(v ?? "").trim()])));
  const byId = Object.fromEntries(rows.map((r) => [r.video_id, r]));
  let n = 0;
  for (const row of state.gt.rows) {
    const r = byId[row.video_id];
    if (!r) continue;
    n++;
    for (const c of state.gt.cols) if (!c.auto && r[c.name] != null) row[c.name] = r[c.name];
  }
  state.gtErrors = [];
  await flushTruth();
  await renderTruth();
  if ($("#gt-msg")) $("#gt-msg").textContent = state.gtSaveError
    ? `Imported ${n} of ${state.gt.rows.length} videos, but the draft was not saved: ${state.gtSaveError}`
    : `Imported results for ${n} of ${state.gt.rows.length} videos (matched by video_id) and saved them as the draft.`;
}

// ------------------------------------------------------------------ reviews: every reviewer at a glance
const CALL = { SCALE: "Scale", DO_NOT_SCALE: "Don't scale" };

async function renderReviews() {
  const body = $("#eval-body");
  let rv, ds;
  try { [rv, ds] = await Promise.all([api("/api/reviews"), api("/api/dataset")]); }
  catch (_) { body.innerHTML = `<section class="panel"><p>Open a campaign in the Dataset tab first.</p></section>`; return; }
  if (state.view !== "eval" || state.evalTab !== "reviews") return;
  state.rv = rv;
  const name = esc(ds.session?.name || "this campaign");
  if (!rv.videos.length) {
    body.innerHTML = `<section class="panel"><h3>Reviews · ${name}</h3>
      <p>Generate the campaign's dataset in the Dataset tab first: reviews are made per video.</p></section>`;
    return;
  }
  const done = rv.reviewers.filter((p) => p.submitted_at);
  const pct = (a, n) => (n ? `${Math.round((a / n) * 100)}%` : "—");
  const cell = (c) => c ? `<span class="chip ${esc(c.decision)}" title="${esc([c.reason, c.minutes && `${c.minutes} min`].filter(Boolean).join(" · "))}">${esc(CALL[c.decision] || label(c.decision))}</span>
      ${c.reason ? `<div class="cap rv-reason">${esc(c.reason)}</div>` : ""}` : '<span class="na">—</span>';
  body.innerHTML = `<section class="panel"><h3>Reviewers · ${name}</h3>
    ${rv.reviewers.length ? `<div class="fwrap"><table class="rv">
      <tr><th>Reviewer</th><th>Status</th><th>Reviewed</th><th>Scale</th><th>Minutes (total · per video)</th>
        <th>${rv.ground_truth_locked ? "Matches ground truth" : "Matches ground truth <span class=\"hint\">(after lock)</span>"}</th><th></th></tr>
      ${rv.reviewers.map((p) => `<tr>
        <td><button class="linkish" data-rvsheet="${esc(p.reviewer)}" title="Open ${esc(p.reviewer)}'s sheet">${esc(p.reviewer)}</button></td>
        <td>${p.submitted_at ? `<span class="chip SCALE">🔒 Submitted</span> <span class="hint">${esc(localTime(p.submitted_at))}</span>
            ${p.scored_at_submit ? `<div class="warn-text cap">after ${p.scored_at_submit} videos were scored</div>` : ""}`
          : '<span class="chip muted">Draft</span>'}</td>
        <td>${p.reviewed} / ${p.videos}</td>
        <td>${p.scale == null ? "—" : `${p.scale} <span class="hint">(${pct(p.scale, p.reviewed)})</span>`}</td>
        <td>${p.total_minutes == null ? "—" : `${p.total_minutes} · ${p.mean_minutes}`}</td>
        <td>${p.correct == null ? "—" : `<b>${p.correct} / ${p.judged}</b> <span class="hint">(${pct(p.correct, p.judged)})</span>`}</td>
        <td class="rv-del">${state.rvDelete === p.reviewer
          ? `<span class="warn-text">Delete ${esc(p.reviewer)}'s review${p.submitted_at ? " (submitted)" : ""} for good?</span>
             <button class="btn small danger" data-rvdelyes="${esc(p.reviewer)}">Delete</button>
             <button class="btn ghost small" data-rvdelno>Cancel</button>`
          : `<button class="btn ghost small" data-rvdel="${esc(p.reviewer)}" aria-label="Delete ${esc(p.reviewer)}'s review">✕</button>`}</td></tr>`).join("")}
    </table></div>` : `<p>No reviewer yet. Type a name in the <b>Reviewer</b> field at the top and fill in
      <button class="linkish" data-evaltab="manual">Manual review</button>.</p>`}
    <p class="hint">A draft's decisions stay hidden until it is submitted, so nobody copies another reviewer's calls.
      Click a name to open that sheet.${rv.agreement.n ? ` Reviewers agree on <b>${rv.agreement.agree} of ${rv.agreement.n}</b>
      videos decided by two or more (${pct(rv.agreement.agree, rv.agreement.n)}).` : ""}</p></section>
    <p id="rv-msg" class="msg"></p>
    <section class="panel"><h3>Decisions per video</h3>
    ${done.length ? `<div class="fwrap"><table class="rv rv-grid">
      <tr><th>Video</th>${done.map((p) => `<th>${esc(p.reviewer)}</th>`).join("")}<th>Agreement</th>
        ${rv.ground_truth_locked ? "<th>Expected</th>" : ""}</tr>
      ${rv.videos.map((v, i) => `<tr><td><button class="vplay" data-rvplay="${i}" title="Watch this video"
            ${!v.url && !v.clip ? "disabled" : ""}>▶ ${esc(v.video_id)}</button>
          <div class="cap">@${esc(v.creator || "—")} · ${esc((v.caption || "").slice(0, 60))}</div></td>
        ${v.merged ? `<td colspan="${done.length}">${cell(v.merged)}
            <div class="cap">same call from ${v.merged.count} reviewers: ${v.merged.reviewers.map(esc).join(", ")}</div></td>`
          : done.map((p) => `<td>${cell(v.calls[p.reviewer])}</td>`).join("")}
        <td>${v.agreement === "agree" ? '<span class="chip SCALE">Agree</span>' : v.agreement === "split" ? '<span class="chip DEFER_TO_HUMAN">Split</span>' : '<span class="na">—</span>'}</td>
        ${rv.ground_truth_locked ? `<td>${v.expected ? `<span class="chip ${esc(v.expected)}">${esc(label(v.expected))}</span>` : '<span class="na">—</span>'}</td>` : ""}</tr>`).join("")}
    </table></div>` : "<p>No submitted review yet: decisions appear here once a reviewer submits.</p>"}</section>`;
}

// ------------------------------------------------------------------ manual review (blind human baseline)
const reviewerName = () => $("#reviewer").value.trim();
const BLIND_METRICS = [["hours_since_post", "h since post"], ["reach", "reach"], ["views", "views"], ["likes", "likes"],
  ["comments", "comments"], ["shares", "shares"], ["saved", "saved"], ["total_interactions", "interactions"]];

function blindSignals(r) {
  const parts = BLIND_METRICS.map(([k, name]) => `<span>${r[k] == null ? "n/a" : esc(int(r[k]))} ${name}</span>`);
  parts.push(`<span>${r.avg_watch_time_ms == null ? "n/a" : (r.avg_watch_time_ms / 1000).toFixed(1) + "s"} avg watch</span>`,
    `<span>${r.skip_rate_pct == null ? "n/a" : esc(r.skip_rate_pct) + "%"} skip</span>`);
  if (/^https?:\/\//i.test(r.video_url || "")) parts.push(`<a href="${esc(r.video_url)}" target="_blank" rel="noopener noreferrer">open post ↗</a>`);
  return `<div class="signals">${parts.join("")}</div>`;
}

function mrCell(i, c, v, done) {
  v = v ?? "";
  if (done) return `<span class="${v ? "" : "na"}">${esc(label(v) || "—")}</span>`;
  const err = state.mrErrors.find((e) => e.row === i && e.column === c.name);
  const a = `class="fc mc k-${c.kind}${err ? " invalid" : ""}" data-mr="${i}" data-col="${esc(c.name)}"
    aria-label="${esc(c.label)}, ${esc(state.mr.rows[i].video_id)}"${err ? ` title="${esc(err.message)}" aria-invalid="true"` : ""}`;
  if (c.kind === "enum") return `<select ${a}><option value=""></option>${c.options.map((o) =>
    `<option ${o.toLowerCase() === String(v).toLowerCase() ? "selected" : ""} value="${esc(o)}">${esc(label(o))}</option>`).join("")}</select>`;
  if (c.kind === "long") return `<textarea ${a} rows="2" placeholder="${esc(c.hint)}">${esc(v)}</textarea>`;
  return `<input type="text" ${a} inputmode="decimal" value="${esc(v)}" placeholder="${esc(c.hint)}">`;
}

async function renderManual() {
  const body = $("#eval-body"), who = reviewerName();
  if (!who) {
    body.innerHTML = `<section class="panel"><h3>Manual review</h3>
      <p>Type your name in the <b>Reviewer</b> field at the top first: each reviewer fills in their own sheet.</p></section>`;
    return;
  }
  let m, ds;
  try { [m, ds] = await Promise.all([api(`/api/manual-review?reviewer=${encodeURIComponent(who)}`), api("/api/dataset")]); }
  catch (_) { body.innerHTML = `<section class="panel"><p>Open a campaign in the Dataset tab first.</p></section>`; return; }
  if (state.view !== "eval" || state.evalTab !== "manual") return;
  if (state.mr?.reviewer !== m.reviewer) { state.mrErrors = []; state.mrStart = {}; }
  state.mr = m; state.mrErrors ||= []; state.mrStart ||= {};
  const name = esc(ds.session?.name || "this campaign");
  if (!m.rows.length) {
    body.innerHTML = `<section class="panel"><h3>Manual review · ${name}</h3>
      <p>Generate the campaign's dataset in the Dataset tab first: decisions are made per video.</p></section>`;
    return;
  }
  const done = !!m.submitted_at, cols = m.cols.filter((c) => !c.auto), k = m.campaign;
  const others = m.reviewers.filter((r) => r.reviewer !== m.reviewer);
  body.innerHTML = `<section class="panel"><h3>Manual review · ${name} · ${esc(m.reviewer)}</h3>
    ${done ? `<div class="banner lock"><b>🔒 Submitted ${esc(localTime(m.submitted_at))}.</b> These decisions can no longer change;
        they are in <code>manual_review.csv</code> and compared with the AI under Results.<br>
        ${m.scored_at_submit ? `<span class="warn-text">submitted after ${m.scored_at_submit} of ${m.rows.length} videos were already scored: not blind if you looked</span>`
          : "submitted before any video was scored"}</div>`
    : `<p>Decide each video as you would without the AI: <b>scale</b> or <b>don't scale</b>, from the signals below only.
        Do not open the Review tab or Results until you submit. Minutes spent are timed from when you first touch a row
        to when you pick a decision; overwrite them if you prefer. Leave a video blank to skip it.</p>`}
    <p class="brief"><b>${esc(k.brand || "—")}</b> · ${esc(label(k.brand_category))} · ${esc(label(k.product_type))}
      · target CAC ${k.target_cac == null ? "not set" : `<b>$${esc(k.target_cac)}</b>`} · countries ${esc(k.target_countries || "—")}</p>
    ${others.length ? `<p class="hint">Other reviewers: ${others.map((r) => `${esc(r.reviewer)} (${r.submitted_at ? "submitted" : `draft, ${r.rows} rows`})`).join(" · ")}</p>` : ""}
    <div class="fwrap"><table class="form gt mr">
      <tr><th>Video</th><th>Signals</th>${cols.map((c) => `<th title="${esc(c.hint)}">${esc(c.label)}</th>`).join("")}</tr>
      ${m.rows.map((r, i) => `<tr data-mrrow="${i}"><td><b>${esc(r.video_id)}</b>
          <div class="cap">@${esc(r.handle || "—")} · ${r.followers == null ? "n/a" : esc(int(r.followers))} followers${r.media_product_type ? ` · ${esc(r.media_product_type)}` : ""}</div>
          <div class="cap full" title="${esc(r.caption || "")}">${esc(r.caption || "")}</div></td>
        <td>${blindSignals(r)}</td>
        ${cols.map((c) => `<td>${mrCell(i, c, r[c.name], done)}</td>`).join("")}</tr>`).join("")}
    </table></div>
    ${state.mrErrors.length ? `<ul class="ferrs">${state.mrErrors.map((e) => `<li>${esc(e.message)}</li>`).join("")}</ul>` : ""}
    ${done ? "" : `<div class="row" style="margin-top:12px">
        <label class="btn ghost small">Import from manual_review.csv…
          <input type="file" id="mr-import" accept=".csv,text/csv" hidden></label>
        <span class="hint" id="mr-status">Saved as you type, as a draft in the campaign's session.json.</span></div>
      <div class="lockbox">
        <label><input type="checkbox" id="mr-confirm"> I decided from these signals only, without seeing the AI's scores. Once submitted it cannot change.</label>
        <button class="btn" id="mr-submit" disabled>🔒 Submit manual review</button>
        <p id="mr-msg" class="msg"></p></div>`}</section>`;
}

function saveManual() {
  clearTimeout(state.mrSaveId);
  if ($("#mr-status")) $("#mr-status").textContent = "Unsaved changes…";
  state.mrSaveId = setTimeout(flushManual, 700);
}

function flushManual() {
  clearTimeout(state.mrSaveId);
  const { reviewer, rows } = state.mr;
  state.mrSaving = (state.mrSaving || Promise.resolve()).then(async () => {
    try {
      await api("/api/manual-review", { method: "PUT", headers: { "content-type": "application/json" },
        body: JSON.stringify({ reviewer, rows }) });
      if ($("#mr-status")) $("#mr-status").textContent = `Draft saved ${new Date().toLocaleTimeString()}.`;
    } catch (e) { if ($("#mr-status")) $("#mr-status").textContent = `Not saved: ${e.message}`; }
  });
  return state.mrSaving;
}

async function submitManual() {
  $("#mr-submit").disabled = true;
  await flushManual();
  try {
    await api("/api/manual-review/submit", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ reviewer: state.mr.reviewer, rows: state.mr.rows }) });
    state.mrErrors = [];
    renderManual();
  } catch (e) {
    state.mrErrors = e.detail?.errors || [];
    await renderManual();
    if ($("#mr-msg")) $("#mr-msg").textContent = state.mrErrors.length
      ? `Not submitted: ${state.mrErrors.length} problem(s), marked in red.` : `Not submitted: ${e.message}`;
  }
}

// A file may hold several reviewers: take this reviewer's rows, or all rows when none carry their name.
async function importManual(file) {
  const all = parseCsv(await file.text()), who = state.mr.reviewer.toLowerCase();
  const mine = all.filter((r) => (r.reviewer || "").trim().toLowerCase() === who);
  const byId = Object.fromEntries((mine.length ? mine : all).map((r) => [r.video_id, r]));
  let n = 0;
  for (const row of state.mr.rows) {
    const r = byId[row.video_id];
    if (!r) continue;
    n++;
    for (const c of state.mr.cols) if (!c.auto && r[c.name] != null) row[c.name] = r[c.name];
  }
  state.mrErrors = [];
  await flushManual();
  await renderManual();
  if ($("#mr-msg")) $("#mr-msg").textContent = `Imported decisions for ${n} of ${state.mr.rows.length} videos (matched by video_id).`;
}

// ------------------------------------------------------------------ campaign recap (step 10)
function recapPanel(c) {
  const r = c.recap;
  const cut = (x) => `${esc(x.video_id)} from ${fmtTs(x.start_s)} for ${x.length_s.toFixed(1)}s (AI cited ${fmtTs(x.cited_s)})`;
  return `<section class="panel"><h3>${esc(c.brand)} · ${esc(c.campaign_id)} · ${r.duration_s}s</h3>
    <div class="top">
      <div><video controls playsinline preload="metadata" src="${esc(r.video_url)}">
        <track kind="captions" srclang="${esc(r.language)}" label="Narration" src="${esc(r.captions_url)}"></video></div>
      <div>
        <dl class="features">
          <dt>Top-scored video</dt><dd>${esc(r.top_video)}</dd>
          <dt>Do-not-scale example</dt><dd>${esc(r.do_not_scale_video || "none in this campaign")}</dd>
          <dt>Clips</dt><dd>${Object.values(r.cuts).flat().map(cut).join("<br>")}</dd>
          <dt>Voice</dt><dd>${esc(r.voice || "macOS say")} · ${esc(r.language)}${r.tempo > 1 ? ` · sped up ×${r.tempo}` : ""}</dd>
          <dt>Generated</dt><dd>${esc(r.generated_at)}</dd>
        </dl>
        <h3 style="margin-top:16px">Narration</h3>
        <ol class="reasons">${Object.values(r.script).map((s) => `<li>${esc(s)}</li>`).join("")}</ol>
        <p class="hint">Captions are burned into the video; the same text is in the
          <a href="${esc(r.captions_url)}" download>.vtt file</a>. <a href="${esc(r.video_url)}" download>Download mp4</a> ·
          <a href="${esc(r.chart_url)}" target="_blank" rel="noopener">chart image</a></p>
      </div>
    </div></section>`;
}

async function renderRecap(message = "") {
  main.innerHTML = `<p class="empty">Loading…</p>`;
  let cs;
  try { cs = await api("/api/campaigns"); }
  catch (e) { main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  if (state.view !== "recap") return;
  main.innerHTML = `
    <section class="panel"><h3>Campaign recap videos</h3>
      <table><tr><th>Campaign</th><th>Brand</th><th>Videos scored</th><th>Recap</th><th></th></tr>
      ${cs.map((c) => `<tr><td>${esc(c.campaign_id)}</td><td>${esc(c.brand)} · ${esc(c.brand_category)}</td>
        <td class="num">${c.scored} / ${c.videos}</td>
        <td>${c.recap ? `${c.recap.duration_s}s · ${esc(c.recap.generated_at)}` : "not generated"}</td>
        <td><button class="btn ${c.recap ? "ghost" : ""} small" data-recap="${esc(c.campaign_id)}"
          data-force="${c.recap ? "true" : "false"}" ${c.scored < c.videos ? "disabled" : ""}>
          ${c.recap ? "Regenerate" : "Generate recap"}</button></td></tr>`).join("")}</table>
      <p class="hint">A recap needs every video of the campaign scored. It shows the top-scored video and one
        do-not-scale example, cut at the timestamps the AI cited, with a narration that only uses numbers from the data.</p>
      <p id="recap-msg" class="msg">${esc(message)}</p></section>
    ${cs.filter((c) => c.recap).map(recapPanel).join("")}`;
}

async function makeRecap(btn) {
  btn.disabled = true;
  $("#recap-msg").textContent = `Writing the script, recording the voice and assembling ${btn.dataset.recap}… (about 30s)`;
  try {
    await api(`/api/campaigns/${encodeURIComponent(btn.dataset.recap)}/recap?force=${btn.dataset.force}`, { method: "POST" });
    renderRecap();
  } catch (e) { renderRecap(`Failed for ${btn.dataset.recap}: ${e.message}`); }
}

// ------------------------------------------------------------------ events
main.addEventListener("change", (ev) => {
  if (ev.target.id === "run-prompt") { state.runPrompt = ev.target.value; refreshRunLine(); }
  if (ev.target.name === "llm-which") saveLlm(ev.target.value);
  if (ev.target.id === "coll-unique" && state.coll) { state.collUnique = ev.target.checked; openCollection(state.coll.cid, "reviews"); }
});
main.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  if (t.dataset.t != null) return seek(+t.dataset.t);
  if (t.dataset.action) return decide(t.dataset.action);
  if (t.id === "ov-toggle") return $("#ov").classList.toggle("open");
  if (t.id === "eval-run") return renderResults(true);
  if (t.id === "home-new") { setView("setup"); return resetDataset(); }
  if (t.id === "coll-back") return renderHome();
  if (t.id === "coll-create") return createCollection(t);
  if (t.dataset.openCollection) return openCollection(t.dataset.openCollection);
  if (t.dataset.collTab) return openCollection(t.dataset.cid, t.dataset.collTab);
  if (t.dataset.rvall != null) { state.rvFilter = null; return renderResults(); }
  if (t.id === "th-save" || t.id === "th-reset") return saveThresholds(t.id === "th-reset");
  if (t.id === "llm-save") return saveLlm();
  if (t.dataset.evaltab) { state.evalTab = t.dataset.evaltab; return renderEval(); }
  if (t.dataset.pv) return promptAction(t.dataset.pv, t.dataset.id, t);
  if (t.id === "gt-lock") return lockTruth();
  if (t.dataset.gtplay != null) return playVideo(state.gt.rows[+t.dataset.gtplay]);
  if (t.dataset.rvplay != null) return playVideo(state.rv.videos[+t.dataset.rvplay]);
  if (t.dataset.rvdel) { state.rvDelete = t.dataset.rvdel; return renderReviews(); }
  if (t.dataset.rvdelno != null) { state.rvDelete = null; return renderReviews(); }
  if (t.dataset.rvdelyes) {
    const who = t.dataset.rvdelyes;
    t.disabled = true;
    try {
      await api(`/api/manual-review?reviewer=${encodeURIComponent(who)}`, { method: "DELETE" });
      state.rvDelete = null;
      if (state.mr?.reviewer === who) state.mr = null;
      await renderReviews();
      if ($("#rv-msg")) $("#rv-msg").textContent = `Deleted ${who}'s review.`;
    } catch (e) { t.disabled = false; if ($("#rv-msg")) $("#rv-msg").textContent = `Not deleted: ${e.message}`; }
    return;
  }
  if (t.dataset.rvsheet) {                   // open that reviewer's own sheet
    $("#reviewer").value = t.dataset.rvsheet;
    try { localStorage.setItem("reviewer", t.dataset.rvsheet); } catch (_) { /* private mode */ }
    state.evalTab = "manual";
    return renderEval();
  }
  if (t.id === "mr-submit") return submitManual();
  if (t.id === "generate") return generateDataset();
  if (t.dataset.openSession) return openSession(t.dataset.openSession, t.dataset.then, +t.dataset.scored || 0);
  if (t.dataset.copySession) return copySession(t);
  if (t.dataset.deleteSession) return askDeleteSession(t.dataset.deleteSession);
  if (t.id === "ds-reset") return resetDataset();
  if (t.id === "form-clear") { clearForm(); saveDraft(); renderForm(); $("#form-msg").textContent = ""; return; }
  if (t.id === "ak-upload") return uploadAnswerKeys();
  if (t.dataset.bulkadd) return addBulk(t.dataset.bulkadd, main.querySelector(`[data-bulktext="${CSS.escape(t.dataset.bulkadd)}"]`).value);
  if (t.dataset.addrow || t.dataset.dupvideo) {
    const file = t.dataset.addrow || t.dataset.dupvideo, rows = form.data[file];
    const last = rows[rows.length - 1];
    // a copy keeps the row's video/creator/campaign links, which is what repeats in child tables
    rows.push(t.dataset.dupvideo && last ? { ...blankRow(file), ...Object.fromEntries(formCols(file)
      .filter((c) => c.kind === "ref").map((c) => [c.name, last[c.name]])) } : blankRow(file));
    saveDraft(); renderForm();
    main.querySelector(`[data-file="${CSS.escape(file)}"][data-row="${rows.length - 1}"]`)?.focus();
    return;
  }
  if (t.dataset.delrow != null) {
    form.data[t.dataset.file].splice(+t.dataset.delrow, 1);
    form.errors = form.errors.filter((e) => e.file !== t.dataset.file);   // row numbers shifted
    saveDraft(); renderForm(); refreshRefs();
    return;
  }
  if (t.id === "start") return startPipeline();
  if (t.dataset.recap) return makeRecap(t);
  if (t.id === "tts") {
    if (speechSynthesis.speaking) { speechSynthesis.cancel(); t.textContent = "🔊 Listen to summary"; return; }
    const u = new SpeechSynthesisUtterance(state.card.spoken_summary);
    u.lang = "en-US";
    u.onend = () => (t.textContent = "🔊 Listen to summary");
    t.textContent = "■ Stop";
    speechSynthesis.speak(u);
    return;
  }
  if (t.id === "run") {
    t.disabled = true;
    $("#run-msg").textContent = "Extracting keyframes, transcribing, analysing, scoring… (about 30–60s)";
    try {
      state.card = await api(`/api/videos/${state.videoId}/process`, { method: "POST" });
      renderCard(state.card); loadList();
    } catch (e) { $("#run-msg").textContent = `Failed: ${e.message}`; t.disabled = false; }
  }
});

$("#video-list").addEventListener("click", (ev) => {
  const b = ev.target.closest("button[data-id]");
  if (b) { setView("review"); openVideo(b.dataset.id); }
});

// Sidebar shortcut: show the Dataset tab (URLs, progress) and start from there.
$("#process-all").addEventListener("click", async () => {
  setView("setup"); clearInterval(state.timerId);
  await renderSetup();
  startPipeline();
});

// A URL is saved as soon as the field is left, so it survives a reload before the run.
main.addEventListener("change", async (ev) => {
  const t = ev.target;
  if (t.id === "bulk-video") { state.bulkVideo = t.value; return; }
  if (t.dataset.bulkfile) { const f = t.files[0]; t.value = ""; if (f) addBulk(t.dataset.bulkfile, await f.text()); return; }
  if (t.id === "gt-confirm") { $("#gt-lock").disabled = !t.checked; return; }
  if (t.id === "llm-provider") return renderLlmPicker(t.value);
  if (t.dataset.rvpick != null) {           // ticking names narrows the Human side; none ticked = everyone
    const picked = [...main.querySelectorAll("[data-rvpick]:checked")].map((x) => x.dataset.rvpick);
    state.rvFilter = picked.length ? new Set(picked) : null;
    return renderResults();
  }
  if (t.id === "gt-import") { importTruth(t.files[0]); t.value = ""; return; }
  if (t.id === "mr-confirm") { $("#mr-submit").disabled = !t.checked; return; }
  if (t.id === "mr-import") { importManual(t.files[0]); t.value = ""; return; }
  if (t.matches(".mc")) return;
  if (t.id === "ds-import") { importCsvs([...t.files]); t.value = ""; return; }
  if (t.matches("input.clip")) return uploadClip(t);
  if (t.matches("input.url-file")) return uploadVideoFile(t);
  if (t.matches(".gc")) return;
  if (t.matches(".fc")) {
    // the change is already in form.data (see the input listener); ids feed the dropdowns
    if (t.dataset.file === CAMPAIGN) {
      if (!!$("#ds-form .locked") === campaignReady()) renderForm();   // unlock / lock the rest
      return;
    }
    if (REF_KEY[t.dataset.file] === t.dataset.col) refreshRefs();
    return;
  }
  if (!t.matches("input.url")) return;
  const td = main.querySelector(`td.st[data-id="${CSS.escape(t.dataset.id)}"]`);
  try {
    await api(`/api/videos/${encodeURIComponent(t.dataset.id)}/url`, {
      method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ url: t.value.trim() }) });
    const v = (await listVideos()).find((x) => x.video_id === t.dataset.id);
    if (td && v) td.innerHTML = videoStatus(v, state.job);
  } catch (e) { if (td) td.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
});

// Typing updates the form data directly; the table is not re-rendered, so focus stays put.
main.addEventListener("input", (ev) => {
  const t = ev.target;
  if (t.matches(".mc")) {
    const i = +t.dataset.mr, row = state.mr.rows[i];
    row[t.dataset.col] = t.value;
    if (t.classList.contains("invalid")) { t.classList.remove("invalid"); t.removeAttribute("title"); }
    const start = state.mrStart[row.video_id];
    if (t.dataset.col === "decision" && t.value && start && !String(row.minutes_spent ?? "").trim()) {
      row.minutes_spent = Math.max(0.1, (Date.now() - start) / 60000).toFixed(1);
      const m = main.querySelector(`.mc[data-mr="${i}"][data-col="minutes_spent"]`);
      if (m) m.value = row.minutes_spent;
    }
    return saveManual();
  }
  if (t.matches(".gc")) {
    state.gt.rows[+t.dataset.gt][t.dataset.col] = t.value;
    if (t.classList.contains("invalid")) { t.classList.remove("invalid"); t.removeAttribute("title"); }
    return saveTruth();
  }
  if (!t.matches(".fc")) return;
  form.data[t.dataset.file][+t.dataset.row][t.dataset.col] = t.value;
  if (t.classList.contains("invalid")) { t.classList.remove("invalid"); t.removeAttribute("title"); }
  saveDraft();
});

// Home and a parent's dashboard are outside any session; Dataset, Review, Evaluation and Recap are sub-items
// of the open session (shown with the video list, under a breadcrumb).
function refreshCrumb() {
  $("#crumb").textContent = [state.session?.collection?.name, state.session?.name].filter(Boolean).join(" › ");
}
const SESSION_VIEWS = ["setup", "review", "eval", "recap"];
function setView(v) {
  state.view = v;
  if (v !== "coll") state.coll = null;
  if (!(v === "coll" || v === "prompts") && /^#\/(collections\/|prompts$)/.test(location.hash)) history.replaceState(null, "", location.pathname + location.search);
  const inside = SESSION_VIEWS.includes(v);
  $("#session-tabs").hidden = !inside;
  $(".layout").classList.toggle("wide", !inside);
  $("#tab-home").classList.toggle("active", !inside && v !== "prompts");
  $("#tab-prompts").classList.toggle("active", v === "prompts");
  refreshCrumb();
  for (const name of SESSION_VIEWS) $(`#tab-${name}`).classList.toggle("active", v === name);
}
$("#tab-prompts").addEventListener("click", () => openPromptLibrary());
$("#tab-home").addEventListener("click", () => renderHome());
$("#tab-setup").addEventListener("click", () => { setView("setup"); clearInterval(state.timerId); renderSetup(); });
$("#tab-recap").addEventListener("click", () => { setView("recap"); clearInterval(state.timerId); renderRecap(); });
$("#tab-review").addEventListener("click", () => { setView("review"); state.card ? renderCard(state.card) : (main.innerHTML = '<p class="empty">Select a video.</p>'); });
$("#tab-eval").addEventListener("click", () => { setView("eval"); clearInterval(state.timerId); renderEval(); });
document.querySelectorAll('input[name="mode"]').forEach((r) =>
  r.addEventListener("change", () => { if (state.view === "review" && state.card) renderCard(state.card); }));

const savedName = (() => { try { return localStorage.getItem("reviewer") || ""; } catch (_) { return ""; } })();
$("#reviewer").value = savedName;
$("#reviewer").addEventListener("change", (e) => { try { localStorage.setItem("reviewer", e.target.value); } catch (_) { /* private mode */ } });
$("#reviewer").addEventListener("change", () => { if (state.view === "eval" && state.evalTab === "manual") renderEval(); });

// Manual review timing starts when a row is first touched.
main.addEventListener("focusin", (ev) => {
  const tr = ev.target.closest("tr[data-mrrow]");
  if (tr && state.mr) state.mrStart[state.mr.rows[+tr.dataset.mrrow].video_id] ||= Date.now();
});

routeFromHash();
if (!state.coll && state.view !== "prompts") renderHome();

async function createCollection(btn) {
  const msg = $("#coll-msg"), name = $("#coll-name").value.trim();
  if (!name) { msg.textContent = "Give the parent a name."; return; }
  btn.disabled = true;
  msg.textContent = "Copying the session…";
  try {
    await api("/api/collections", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ source: $("#coll-source").value, name, children: +$("#coll-n").value || 0 }) });
    await renderHome();
  } catch (e) { btn.disabled = false; msg.textContent = `Not created: ${e.message}`; }
}
