// Review card UI. Everything from the video (captions, on-screen text, comments, model
// output) is untrusted, so it only ever reaches the DOM through esc().

const $ = (s, el = document) => el.querySelector(s);
const main = $("#main");
const state = { view: "setup", videoId: null, card: null, startedAt: null, timerId: null,
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
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
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
async function loadList() {
  const vids = await api("/api/videos");
  $("#video-list").innerHTML = vids.map((v) => `
    <li><button data-id="${esc(v.video_id)}" class="${v.video_id === state.videoId ? "active" : ""}">
      <span class="row1"><span>${esc(v.video_id)}${v.decided ? " ✓" : ""}</span>${chip(v.verdict)}</span>
      <span class="cap">${esc(v.brand)} · @${esc(v.creator)}</span>
      <span class="cap">${esc(v.caption)}</span>
    </button></li>`).join("");
}

// ------------------------------------------------------------------ dataset + pipeline run
const STEPS = { queued: "queued", downloading: "downloading video…", preprocessing: "keyframes + transcript…",
  analysing: "visual analysis…", scoring: "scoring…" };

// What one video is doing: the running job's word for it, else where it stands on disk.
function videoStatus(v, job) {
  const j = job?.videos?.[v.video_id];
  if (j?.state === "error") return `<span class="err">${esc(j.error)}</span>`;
  if (j && j.state !== "done") return `<span class="busy">${esc(STEPS[j.state] || j.state)}</span>`;
  if (v.scored) return chip(v.verdict);
  if (v.source_url) return v.has_clip ? "downloaded, not scored" : "URL set, not downloaded";
  return v.has_clip ? "clip in dataset, not scored" : '<span class="na">no video: add a URL</span>';
}

function jobLine(job) {
  if (!job?.started_at) return "";
  const errors = Object.values(job.videos).filter((v) => v.state === "error").length;
  return job.running ? `Running: ${job.done} / ${job.total} videos done`
    : `Finished: ${job.total - errors} of ${job.total} videos processed${errors ? `, ${errors} failed` : ""}.`;
}

async function renderSetup() {
  main.innerHTML = `<p class="empty">Loading…</p>`;
  let ds, vids, job;
  try {
    [ds, vids, job] = await Promise.all([api("/api/dataset"), api("/api/videos"), api("/api/pipeline/status")]);
  } catch (e) { main.innerHTML = `<p class="empty">Error: ${esc(e.message)}</p>`; return; }
  if (state.view !== "setup") return;
  state.job = job;
  const others = ds.uploads.filter((u) => u !== ds.id);
  main.innerHTML = `
    <section class="panel"><h3>1 · Dataset</h3>
      <p>Active: <b>${ds.source === "upload" ? `upload ${esc(ds.id)}` : "example dataset"}</b> ·
        ${ds.videos} videos · ${esc(ds.files.join(", "))}</p>
      <div class="row">
        <input type="file" id="ds-files" multiple accept=".csv,text/csv">
        <button class="btn" id="ds-upload">Upload dataset</button>
        ${ds.source === "upload" ? `<button class="btn ghost" data-use="">Use example dataset</button>` : ""}
        ${others.length ? `<select id="ds-prev" aria-label="Earlier uploads"><option value="">Earlier uploads…</option>
          ${others.map((u) => `<option>${esc(u)}</option>`).join("")}</select>` : ""}
      </div>
      <p class="hint">Select all the CSVs at once. Required: <code>videos.csv</code>, <code>creators.csv</code>,
        <code>campaigns.csv</code>. Optional: <code>comments.csv</code>, <code>media.csv</code>, <code>keyframes.csv</code>,
        <code>transcripts.csv</code>, <code>ground_truth.csv</code>, <code>ground_truth_media.csv</code>.
        A <code>video_url</code> column in <code>videos.csv</code> or <code>media.csv</code> pre-fills the URLs below.</p>
      <p id="ds-msg" class="msg"></p></section>
    <section class="panel"><h3>2 · Video URLs</h3>
      <table class="urls"><tr><th>Video</th><th>Brand · creator</th><th>Video URL</th><th>Status</th></tr>
      ${vids.map((v) => `<tr><td>${esc(v.video_id)}</td><td>${esc(v.brand)} · @${esc(v.creator)}</td>
        <td><input type="url" class="url" data-id="${esc(v.video_id)}" value="${esc(v.source_url || "")}"
          placeholder="https://… (mp4 link, or Instagram / TikTok / YouTube page)"></td>
        <td class="st" data-id="${esc(v.video_id)}">${videoStatus(v, job)}</td></tr>`).join("")}</table>
      <p class="hint">Leave a URL blank to use the clip in the dataset's media folder, when there is one.</p></section>
    <section class="panel"><h3>3 · Run</h3>
      <div class="row">
        <button class="btn" id="start" ${job.running ? "disabled" : ""}>Start pipeline</button>
        <label><input type="checkbox" id="force"> Re-run videos that are already scored</label>
      </div>
      <p id="job-msg" class="msg">${esc(jobLine(job))}</p></section>`;
  state.vids = vids;
  if (job.running) poll();
}

function poll() {
  clearInterval(state.pollId);
  state.pollId = setInterval(async () => {
    let job, vids;
    try { [job, vids] = await Promise.all([api("/api/pipeline/status"), api("/api/videos")]); } catch (_) { return; }
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
  const body = { force: !!$("#force")?.checked, urls };
  if ($("#start")) $("#start").disabled = true;
  try {
    state.job = await api("/api/pipeline/start", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
    if ($("#job-msg")) $("#job-msg").textContent = jobLine(state.job);
    poll();
  } catch (e) {
    if ($("#job-msg")) $("#job-msg").textContent = `Not started: ${e.message}`;
    if ($("#start")) $("#start").disabled = false;
  }
}

async function switchDataset(run) {
  const msg = $("#ds-msg");
  msg.textContent = "Working…";
  try {
    const ds = await run();
    state.videoId = null; state.card = null;
    await Promise.all([renderSetup(), loadList()]);
    if (ds.ignored?.length) $("#ds-msg").textContent = `Ignored (not a known dataset file): ${ds.ignored.join(", ")}`;
  } catch (e) { msg.textContent = `Failed: ${e.message}`; }
}

function uploadDataset() {
  const files = $("#ds-files").files;
  if (!files.length) { $("#ds-msg").textContent = "Choose the CSV files first."; return; }
  const form = new FormData();
  for (const f of files) form.append("files", f);
  switchDataset(() => api("/api/dataset", { method: "POST", body: form }));
}

const useDataset = (id) => switchDataset(() => api("/api/dataset/use", {
  method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ id: id || null }) }));

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
    <ol class="reasons">${s.reasons.map((r) => `<li>${linkTs(esc(r))}</li>`).join("")}</ol>`;
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
      <div class="cap">${esc(c.caption)}</div>${mode() === "card" ? banners(c) : ""}</section>`;

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

async function renderEval(runMissing = false) {
  main.innerHTML = `<p class="empty">${runMissing ? "Running pipeline on unscored videos…" : "Loading…"}</p>`;
  let e;
  try { e = await api(`/api/evaluate?run_missing=${runMissing}`); }
  catch (err) { main.innerHTML = `<p class="empty">Error: ${esc(err.message)}</p>`; return; }
  const cell = (got, exp) => `<td class="${got == null ? "" : got === exp ? "ok" : "bad"}">${esc(label(got))}</td>`;
  const t = e.reviewer_timing;
  main.innerHTML = `
    <section class="panel"><h3>Verdict accuracy vs ground truth</h3>
      <table><tr><th>Prompt</th><th>Correct</th><th>Accuracy</th></tr>
      ${Object.entries(e.verdict_accuracy).map(([k, v]) => `<tr><td>${esc(k)}</td><td class="num">${v.correct} / ${v.n}</td>
        <td class="num">${v.accuracy == null ? "—" : (v.accuracy * 100).toFixed(0) + "%"}</td></tr>`).join("")}</table>
      <p style="font-size:13px">P5 is compared with <code>ground_truth.csv</code>; P6 with the multimodal key in
        <code>ground_truth_media.csv</code>. "After rule guard" applies the 48h / reach / missing-metric rules in code.</p>
      <button class="btn ghost small" id="eval-run">Score any unprocessed videos</button></section>
    <section class="panel"><h3>Per video</h3>
      <table><tr><th>Video</th><th>Scenario</th><th>Expected (text / multimodal)</th><th>P5 model</th><th>P5 final</th>
        <th>P6 model</th><th>P6 final</th><th>P6 reasons citing time</th></tr>
      ${e.videos.map((v) => `<tr><td>${esc(v.video_id)}</td><td>${esc(label(v.scenario))}</td>
        <td>${esc(label(v.expected))} / ${esc(label(v.expected_multimodal))}</td>
        ${cell(v.p5_model, v.expected)}${cell(v.p5_final, v.expected)}
        ${cell(v.p6_model, v.expected_multimodal)}${cell(v.p6_final, v.expected_multimodal)}
        <td class="num">${esc(v.p6_reasons_citing_time ?? "—")}</td></tr>`).join("")}</table></section>
    <section class="panel"><h3>Content features vs ground truth</h3>
      <table><tr><th>Feature</th><th>Correct</th><th>Misses (got → expected)</th></tr>
      ${Object.entries(e.feature_accuracy).map(([k, v]) => `<tr><td>${esc(label(k))}</td><td class="num">${v.correct} / ${v.n}</td>
        <td>${e.videos.filter((x) => x.features[k] && !x.features[k].ok).map((x) =>
          `${esc(x.video_id)}: ${esc(x.features[k].got ?? "none")} → ${esc(x.features[k].expected ?? "none")}`).join("; ") || "—"}</td></tr>`).join("")}</table></section>
    <section class="panel"><h3>Reviewer decision time</h3>
      <div class="stat">${["card", "text"].map((m) => `<div><b>${t[m].median_s == null ? "—" : t[m].median_s + "s"}</b>
        median, ${m === "card" ? "with the card" : "text only"}<br><span style="font-size:13px">${t[m].decisions} decisions ·
        mean ${t[m].mean_s ?? "—"}s · agreed with AI ${t[m].agreed_with_ai}/${t[m].decisions}</span></div>`).join("")}</div>
      <p style="font-size:13px">Timed from the moment a video's view is shown until a decision is logged.
        Switch the mode toggle in the header to collect both conditions.</p></section>`;
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
main.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button");
  if (!t) return;
  if (t.dataset.t != null) return seek(+t.dataset.t);
  if (t.dataset.action) return decide(t.dataset.action);
  if (t.id === "ov-toggle") return $("#ov").classList.toggle("open");
  if (t.id === "eval-run") return renderEval(true);
  if (t.id === "ds-upload") return uploadDataset();
  if (t.dataset.use != null) return useDataset(t.dataset.use);
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
  if (t.id === "ds-prev" && t.value) return useDataset(t.value);
  if (!t.matches("input.url")) return;
  const td = main.querySelector(`td.st[data-id="${CSS.escape(t.dataset.id)}"]`);
  try {
    await api(`/api/videos/${encodeURIComponent(t.dataset.id)}/url`, {
      method: "PUT", headers: { "content-type": "application/json" }, body: JSON.stringify({ url: t.value.trim() }) });
    const v = (await api("/api/videos")).find((x) => x.video_id === t.dataset.id);
    if (td && v) td.innerHTML = videoStatus(v, state.job);
  } catch (e) { if (td) td.innerHTML = `<span class="err">${esc(e.message)}</span>`; }
});

function setView(v) {
  state.view = v;
  for (const name of ["setup", "review", "eval", "recap"]) $(`#tab-${name}`).classList.toggle("active", v === name);
}
$("#tab-setup").addEventListener("click", () => { setView("setup"); clearInterval(state.timerId); renderSetup(); });
$("#tab-recap").addEventListener("click", () => { setView("recap"); clearInterval(state.timerId); renderRecap(); });
$("#tab-review").addEventListener("click", () => { setView("review"); state.card ? renderCard(state.card) : (main.innerHTML = '<p class="empty">Select a video.</p>'); });
$("#tab-eval").addEventListener("click", () => { setView("eval"); clearInterval(state.timerId); renderEval(); });
document.querySelectorAll('input[name="mode"]').forEach((r) =>
  r.addEventListener("change", () => { if (state.view === "review" && state.card) renderCard(state.card); }));

const savedName = (() => { try { return localStorage.getItem("reviewer") || ""; } catch (_) { return ""; } })();
$("#reviewer").value = savedName;
$("#reviewer").addEventListener("change", (e) => { try { localStorage.setItem("reviewer", e.target.value); } catch (_) { /* private mode */ } });

loadList();
renderSetup();
