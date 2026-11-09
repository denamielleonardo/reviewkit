// ReviewKit UI
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
  review: "capataz",
  repo: null,
  docx: null,
  status: null,
  historyId: null,
  running: { code: null, td: null },
};

// ---------- helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body && !(opts.body instanceof FormData) ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body && !(opts.body instanceof FormData) ? JSON.stringify(opts.body) : opts.body,
  });
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = j.detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res;
}
const getJSON = async (p, o) => (await api(p, o)).json();

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), bad ? 5000 : 2600);
}

function busy(btn, on, label) {
  if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; if (label) btn.innerHTML = `<span class="spinner"></span>${label}`; }
  else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
}

// ---------- theme ----------
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "system") delete root.dataset.theme; else root.dataset.theme = mode;
  try { localStorage.setItem("rk-theme", mode); } catch (e) {}
  $$("[data-theme-set]").forEach((b) => b.classList.toggle("active", b.dataset.themeSet === mode));
}
function initTheme() {
  let mode = "system";
  try { mode = localStorage.getItem("rk-theme") || "system"; } catch (e) {}
  applyTheme(mode);
  $$("[data-theme-set]").forEach((b) => b.addEventListener("click", () => {
    applyTheme(b.dataset.themeSet);
    api("/api/settings", { method: "POST", body: { theme: b.dataset.themeSet } }).catch(() => {});
  }));
}

// ---------- navigation ----------
function show(view) {
  $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  if (view === "history") loadHistory();
  if (view === "settings") loadSettings();
}
$$("[data-view]").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));

// ---------- account ----------
function describeAuth(a) {
  if (!a) return { ok: false, title: "Unknown", sub: "" };
  if (a.authMethod === "api_key") return { ok: true, title: "API key", sub: "Anthropic Console key" };
  if (a.loggedIn) {
    const plan = a.subscriptionType ? a.subscriptionType[0].toUpperCase() + a.subscriptionType.slice(1) : "Claude";
    return { ok: true, title: `Connected · ${plan}`, sub: a.email || a.orgName || "" };
  }
  return { ok: false, title: "Not signed in", sub: "Click to connect Claude" };
}

async function loadStatus() {
  try {
    state.status = await getJSON("/api/status");
  } catch (e) {
    state.status = null;
  }
  const d = describeAuth(state.status && state.status.auth);
  $("#accountDot").className = "dot " + (d.ok ? "ok" : "bad");
  $("#accountTitle").textContent = d.title;
  $("#accountSub").textContent = d.sub;
  const s = $("#authStatus");
  s.innerHTML = `<span class="dot ${d.ok ? "ok" : "bad"}"></span><span>${d.title}</span><span class="muted">${d.sub || ""}</span>`;
  $("#loginBtn").classList.toggle("hidden", d.ok && state.status.auth.authMethod !== "api_key" ? true : false);
  $("#logoutBtn").classList.toggle("hidden", !(d.ok && state.status.auth.authMethod !== "api_key"));
  if (state.status && !state.status.git) toast("Git was not found. Install Git for Windows to run code reviews.", true);
  return d.ok;
}

// ---------- code review ----------
function renderRecent(repos) {
  const box = $("#recentRepos");
  box.innerHTML = "";
  (repos || []).slice(0, 5).forEach((r) => {
    const c = document.createElement("button");
    c.className = "chip";
    c.title = r;
    c.textContent = r.split(/[\\/]/).filter(Boolean).pop();
    c.onclick = () => { $("#repoPath").value = r; openRepo(); };
    box.appendChild(c);
  });
}

function setBranches(list) {
  $("#branchList").innerHTML = list.map((b) => `<option value="${b.replace(/"/g, "&quot;")}">`).join("");
}

async function openRepo() {
  const path = $("#repoPath").value.trim();
  if (!path) return toast("Enter or browse to your local clone first.", true);
  const btn = $("#openRepoBtn");
  busy(btn, true, "Opening");
  try {
    const info = await getJSON("/api/repo", { method: "POST", body: { path } });
    state.repo = info;
    $("#repoPath").value = info.root;
    $("#repoName").textContent = info.name;
    $("#repoOrigin").textContent = info.origin || "no origin remote";
    $("#repoInfo").classList.remove("hidden");
    setBranches(info.branches);
    const tgt = $("#targetBranch");
    if (!tgt.value) tgt.value = ["develop", "dev", "main", "master"].find((b) => info.branches.includes(b)) || "";
    const s = await getJSON("/api/settings");
    renderRecent(s.repos);
    if (!info.branches.length) fetchRepo();
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy(btn, false);
  }
}

async function fetchRepo() {
  if (!state.repo) return;
  const btn = $("#fetchBtn");
  busy(btn, true, "Fetching");
  try {
    const r = await getJSON("/api/repo/fetch", { method: "POST", body: { path: state.repo.root } });
    setBranches(r.branches);
    toast(`Fetched · ${r.branches.length} branches`);
  } catch (e) {
    toast(e.message, true);
  } finally {
    busy(btn, false);
  }
}

$("#openRepoBtn").onclick = openRepo;
$("#repoPath").addEventListener("keydown", (e) => { if (e.key === "Enter") openRepo(); });
$("#fetchBtn").onclick = fetchRepo;
$("#browseBtn").onclick = async () => {
  try {
    const r = await getJSON("/api/pick-folder", { method: "POST" });
    if (r.path) { $("#repoPath").value = r.path; openRepo(); }
  } catch (e) { toast(e.message, true); }
};
$("#swapBtn").onclick = () => {
  const s = $("#sourceBranch"), t = $("#targetBranch");
  [s.value, t.value] = [t.value, s.value];
};
$$("#reviewType button").forEach((b) => b.addEventListener("click", () => {
  state.review = b.dataset.review;
  $$("#reviewType button").forEach((x) => x.classList.toggle("active", x === b));
}));

// ---------- result panel ----------
function makeResult(slotName, title, sub) {
  const slot = $(`[data-slot="${slotName}"]`);
  slot.innerHTML = "";
  const node = $("#resultTpl").content.firstElementChild.cloneNode(true);
  slot.appendChild(node);
  $(".r-title", node).textContent = title;
  $(".r-sub", node).textContent = sub || "";
  const panel = {
    node, markdown: "", entry: null, controller: null,
    body: $(".r-body", node),
    setState(s) { node.classList.remove("running", "done", "error"); node.classList.add(s); },
    setSub(t) { $(".r-sub", node).textContent = t; },
  };
  $(".r-copy", node).onclick = async () => {
    if (!panel.markdown) return toast("Nothing to copy yet.", true);
    try { await navigator.clipboard.writeText(panel.markdown); toast("Copied Markdown to clipboard"); }
    catch (e) {
      const ta = document.createElement("textarea"); ta.value = panel.markdown; document.body.appendChild(ta);
      ta.select(); document.execCommand("copy"); ta.remove(); toast("Copied Markdown to clipboard");
    }
  };
  $(".r-export", node).onclick = async () => {
    if (!panel.entry) return toast("Export is available once the review has finished.", true);
    try {
      const r = await getJSON(`/api/history/${panel.entry.id}/export`, { method: "POST" });
      if (r.ok) toast(`Saved to ${r.path}`);
    } catch (e) { toast(e.message, true); }
  };
  return panel;
}

function showStatic(slotName, entry) {
  const p = makeResult(slotName, entry.title, `${entry.label} · ${entry.created}`);
  p.markdown = entry.markdown;
  p.entry = entry;
  p.setState("done");
  p.body.innerHTML = renderMarkdown(entry.markdown);
  attachApply(p, entry);
  return p;
}

async function streamReview(slotName, title, sub, request, runBtn) {
  const p = makeResult(slotName, title, sub);
  const activity = $(".activity", p.node), log = $(".activity-log", p.node), now = $(".activity-now", p.node);
  const stop = $(".r-stop", p.node);
  p.setState("running");
  activity.classList.remove("hidden");
  stop.classList.remove("hidden");
  const controller = new AbortController();
  stop.onclick = () => controller.abort();
  state.running[slotName] = controller;
  busy(runBtn, true, "Reviewing");
  const started = Date.now();
  const tick = setInterval(() => p.setSub(`${sub} · ${Math.round((Date.now() - started) / 1000)}s`), 1000);

  let live = "";
  let tools = 0;
  let frame = 0;
  // The hidden REVIEW_POINTS block (TD reviews) is never shown while streaming.
  const visible = () => { const i = live.indexOf("<!--"); return i >= 0 ? live.slice(0, i) : live; };
  const paint = () => { frame = 0; p.body.innerHTML = renderMarkdown(visible()); p.body.lastElementChild && p.body.lastElementChild.classList.add("cursor"); };

  try {
    const res = await api(request.url, { method: "POST", body: request.body, signal: controller.signal });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.t === "reset") { live = ""; }
        else if (ev.t === "text") { live += ev.v; if (!frame) frame = setTimeout(paint, 80); }
        else if (ev.t === "tool") {
          tools++;
          now.textContent = ev.v;
          const li = document.createElement("li"); li.textContent = ev.v; log.appendChild(li);
          log.scrollTop = log.scrollHeight;
        } else if (ev.t === "done") {
          clearTimeout(frame); frame = 0;
          p.markdown = ev.markdown || live;
          p.entry = ev.entry || null;
          p.body.innerHTML = renderMarkdown(p.markdown);
          p.setState("done");
          now.textContent = `Finished in ${ev.seconds}s · ${tools} lookups`;
          p.setSub(`${sub} · ${ev.seconds}s`);
          toast("Review complete");
          attachApply(p, ev.entry);
        } else if (ev.t === "error") {
          throw new Error(ev.v);
        }
      }
    }
    if (!p.node.classList.contains("done")) throw new Error("The review ended without a result.");
  } catch (e) {
    p.setState("error");
    const aborted = e.name === "AbortError";
    now.textContent = aborted ? "Stopped" : "Failed";
    if (aborted && live) { p.markdown = live; p.body.innerHTML = renderMarkdown(live); }
    else p.body.innerHTML = `<div class="error-box"></div>`, $(".error-box", p.body).textContent = e.message;
    if (!aborted) toast("Review failed", true);
  } finally {
    clearInterval(tick);
    stop.classList.add("hidden");
    state.running[slotName] = null;
    busy(runBtn, false);
    if (slotName === "td") $("#runTdBtn").disabled = !state.docx;
  }
}

$("#runCodeBtn").onclick = async () => {
  if (!state.repo) return toast("Open a local repository first.", true);
  const source = $("#sourceBranch").value.trim(), target = $("#targetBranch").value.trim();
  if (!source || !target) return toast("Enter both a source and a target branch.", true);
  if (source === target) return toast("Source and target must be different branches.", true);
  const label = $(`#reviewType button[data-review="${state.review}"]`).textContent.trim();
  streamReview("code", `${source} → ${target}`, `${state.repo.name} · ${label}`, {
    url: "/api/review/code",
    body: { repo: state.repo.root, source, target, review: state.review, notes: $("#codeNotes").value },
  }, $("#runCodeBtn"));
};

// ---------- technical design ----------
const dz = $("#dropzone");
function setDocx(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".docx")) return toast("Please choose a .docx file.", true);
  state.docx = file;
  dz.classList.add("has-file");
  $("#dzTitle").textContent = file.name;
  $("#dzSub").textContent = `${(file.size / 1024).toFixed(0)} KB · click to choose a different file`;
  $("#runTdBtn").disabled = false;
}
$("#docxInput").addEventListener("change", (e) => setDocx(e.target.files[0]));
["dragenter", "dragover"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.add("over"); }));
["dragleave", "drop"].forEach((t) => dz.addEventListener(t, (e) => { e.preventDefault(); dz.classList.remove("over"); }));
dz.addEventListener("drop", (e) => setDocx(e.dataTransfer.files[0]));

$("#runTdBtn").onclick = () => {
  if (!state.docx) return;
  const fd = new FormData();
  fd.append("file", state.docx);
  fd.append("notes", $("#tdNotes").value);
  streamReview("td", state.docx.name, "Technical Design", { url: "/api/review/td", body: fd }, $("#runTdBtn"));
};

// ---------- apply TD review points ----------
const SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3 };
const STATUS_LABEL = {
  applied: "Applied", not_applicable: "Not applicable", needs_input: "Needs author input",
  failed: "Not applied", open: "Open",
};
const sevClass = (s) => "sev sev-" + ((s || "").toLowerCase().match(/critical|high|medium|low/) || ["none"])[0];
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function attachApply(panel, entry) {
  if (!entry || entry.kind !== "td" || !(entry.points || []).length) return;
  let box = $(".apply", panel.node);
  if (!box) { box = document.createElement("div"); box.className = "apply"; panel.node.appendChild(box); }
  const points = entry.points;
  const done = points.some((p) => p.status && p.status !== "open");
  if (done) return renderApplyResult(panel, box, entry, points);

  box.innerHTML = `
    <div class="apply-offer">
      <div class="apply-icon"><svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z"/><path d="M14 3v5h5M9 15l2 2 4-4"/></svg></div>
      <div class="apply-text">
        <b>Want ReviewKit to apply these review points to the document?</b>
        <span class="muted small">You choose which points to apply. Edits follow your document's template and appear as tracked changes in a copy. Your original isn't modified.</span>
      </div>
      <div class="row">
        <button class="btn ghost a-no">No thanks</button>
        <button class="btn primary a-yes">Yes, choose points</button>
      </div>
    </div>`;
  $(".a-no", box).onclick = () => { box.innerHTML = `<div class="apply-note muted small">OK, the document was left as is. You can apply points later from History.</div>`; };
  $(".a-yes", box).onclick = () => renderApplySelect(panel, box, entry, points);
}

function renderApplySelect(panel, box, entry, points) {
  const sorted = [...points].sort((a, b) => (SEV_ORDER[(a.severity || "").toLowerCase()] ?? 9) - (SEV_ORDER[(b.severity || "").toLowerCase()] ?? 9));
  box.innerHTML = `
    <div class="apply-head">
      <div><b>Choose the review points to apply</b>
        <div class="muted small">Untick anything that doesn't apply. Unticked points are marked <b>Not applicable</b>, and their parts of the document aren't touched.</div></div>
      <div class="row"><button class="btn tiny ghost a-all">Select all</button><button class="btn tiny ghost a-none">Select none</button></div>
    </div>
    <div class="point-list">${sorted.map((p) => `
      <label class="point">
        <input type="checkbox" value="${escapeHtml(p.id)}" checked>
        <div class="point-body">
          <div class="point-top"><span class="${sevClass(p.severity)}">${escapeHtml(p.severity || "—")}</span>
            <span class="point-id">${escapeHtml(p.id)}</span><b>${escapeHtml(p.title)}</b></div>
          ${p.section ? `<div class="muted small">Section: ${escapeHtml(p.section)}</div>` : ""}
          <div class="point-change">${escapeHtml(p.change)}</div>
        </div>
      </label>`).join("")}
    </div>
    <div class="apply-foot">
      <span class="muted small a-count"></span>
      <div class="row"><button class="btn ghost a-cancel">Cancel</button><button class="btn primary a-go"></button></div>
    </div>`;
  const boxes = $$("input[type=checkbox]", box);
  const sync = () => {
    const n = boxes.filter((b) => b.checked).length;
    $(".a-count", box).textContent = `${n} of ${boxes.length} selected · ${boxes.length - n} will be marked Not applicable`;
    const go = $(".a-go", box);
    go.textContent = n ? `Apply ${n} point${n > 1 ? "s" : ""}` : "Select at least one point";
    go.disabled = !n;
    boxes.forEach((b) => b.closest(".point").classList.toggle("off", !b.checked));
  };
  boxes.forEach((b) => b.addEventListener("change", sync));
  $(".a-all", box).onclick = () => { boxes.forEach((b) => (b.checked = true)); sync(); };
  $(".a-none", box).onclick = () => { boxes.forEach((b) => (b.checked = false)); sync(); };
  $(".a-cancel", box).onclick = () => attachApply(panel, { ...entry, points: points.map((p) => ({ ...p, status: "open" })) });
  $(".a-go", box).onclick = () => runApply(panel, box, entry, boxes.filter((b) => b.checked).map((b) => b.value));
  sync();
}

async function runApply(panel, box, entry, selected) {
  box.innerHTML = `<div class="apply-running"><span class="spinner"></span><span class="a-status">Starting…</span></div>`;
  try {
    const res = await api(`/api/td/${entry.id}/apply`, { method: "POST", body: { selected } });
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        if (!line.trim()) continue;
        const ev = JSON.parse(line);
        if (ev.t === "status") $(".a-status", box).textContent = ev.v;
        else if (ev.t === "error") throw new Error(ev.v);
        else if (ev.t === "done") {
          panel.markdown = ev.markdown;
          panel.entry = ev.entry;
          panel.body.innerHTML = renderMarkdown(ev.markdown);
          renderApplyResult(panel, box, ev.entry, ev.points);
          toast("Document updated with tracked changes");
        }
      }
    }
  } catch (e) {
    box.innerHTML = `<div class="error-box"></div><div class="row" style="margin-top:10px"><button class="btn ghost a-retry">Back to selection</button></div>`;
    $(".error-box", box).textContent = e.message;
    $(".a-retry", box).onclick = () => renderApplySelect(panel, box, entry, entry.points);
  }
}

function renderApplyResult(panel, box, entry, points) {
  const count = (s) => points.filter((p) => p.status === s).length;
  box.innerHTML = `
    <div class="apply-head">
      <div><b>${escapeHtml(entry.reviewed_name || "Reviewed document")}</b>
        <div class="muted small">${count("applied")} applied · ${count("not_applicable")} not applicable${count("needs_input") ? ` · ${count("needs_input")} need author input` : ""}${count("failed") ? ` · ${count("failed")} not applied` : ""}. Review the tracked changes in Word to accept or reject them.</div></div>
      <div class="row">
        <button class="btn tiny ghost a-change">Change selection</button>
        <button class="btn tiny ghost a-save">Save a copy…</button>
        <button class="btn tiny primary a-open">Open in Word</button>
      </div>
    </div>
    <div class="point-list">${points.map((p) => `
      <div class="point result-${p.status}">
        <span class="status-pill st-${p.status}">${STATUS_LABEL[p.status] || p.status}</span>
        <div class="point-body">
          <div class="point-top"><span class="${sevClass(p.severity)}">${escapeHtml(p.severity || "—")}</span>
            <span class="point-id">${escapeHtml(p.id)}</span><b>${escapeHtml(p.title)}</b></div>
          ${p.note ? `<div class="point-change">${escapeHtml(p.note)}</div>` : ""}
        </div>
      </div>`).join("")}
    </div>`;
  $(".a-open", box).onclick = async () => { try { await api(`/api/td/${entry.id}/open`, { method: "POST" }); } catch (e) { toast(e.message, true); } };
  $(".a-save", box).onclick = async () => {
    try { const r = await getJSON(`/api/td/${entry.id}/save`, { method: "POST" }); if (r.ok) toast(`Saved to ${r.path}`); }
    catch (e) { toast(e.message, true); }
  };
  $(".a-change", box).onclick = () => renderApplySelect(panel, box, entry, points);
}

// ---------- history ----------
let historyItems = [];
function renderHistory() {
  const q = $("#historySearch").value.toLowerCase();
  const list = $("#historyList");
  const items = historyItems.filter((e) => !q || `${e.title} ${e.label}`.toLowerCase().includes(q));
  if (!items.length) { list.innerHTML = `<div class="empty">${historyItems.length ? "No matches." : "No reviews yet."}</div>`; return; }
  list.innerHTML = "";
  items.forEach((e) => {
    const b = document.createElement("button");
    b.className = "h-item" + (e.id === state.historyId ? " active" : "");
    b.innerHTML = `<div class="h-title"></div><div class="h-meta"><span class="tag"></span><span></span></div>`;
    $(".h-title", b).textContent = e.title;
    $(".tag", b).textContent = e.label;
    $(".h-meta span:last-child", b).textContent = e.created;
    b.onclick = async () => {
      state.historyId = e.id;
      renderHistory();
      const full = await getJSON(`/api/history/${e.id}`);
      const p = showStatic("history", full);
      const del = document.createElement("button");
      del.className = "btn tiny ghost danger";
      del.textContent = "Delete";
      del.onclick = async () => {
        if (!confirm("Delete this review from history?")) return;
        await api(`/api/history/${e.id}`, { method: "DELETE" });
        state.historyId = null;
        $('[data-slot="history"]').innerHTML = "";
        loadHistory();
      };
      $(".result-head .row", p.node).appendChild(del);
    };
    list.appendChild(b);
  });
}
async function loadHistory() {
  try { historyItems = await getJSON("/api/history"); } catch (e) { historyItems = []; }
  renderHistory();
}
$("#historySearch").addEventListener("input", renderHistory);

// ---------- settings ----------
async function loadSettings() {
  loadStatus();
  const s = await getJSON("/api/settings");
  $("#keyHint").textContent = s.api_key_set ? `A key ending in ${s.api_key_hint} is saved.` : "No API key saved.";
  $$("#modelSel button").forEach((b) => b.classList.toggle("active", b.dataset.model === (s.model || "")));
  const st = state.status;
  if (st) {
    $("#about").innerHTML = st.reviews.map((r) => `<div class="skill"><b></b><span class="muted small"></span></div>`).join("") +
      `<div class="skill"><b>ReviewKit</b><span class="muted small">Version ${st.version}</span></div>`;
    $$("#about .skill").forEach((el, i) => {
      if (!st.reviews[i]) return;
      $("b", el).textContent = st.reviews[i].label;
      $("span", el).textContent = `Embedded skill: ${st.reviews[i].skill}`;
    });
  }
}

$("#loginBtn").onclick = async () => {
  try {
    await api("/api/login", { method: "POST" });
    toast("Finish signing in in your browser. This page updates automatically.");
    // Poll until the sign-in completes (up to 5 minutes), then refresh the UI.
    for (let i = 0; i < 100; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      if (await loadStatus()) { toast("Connected to Claude"); return; }
    }
  } catch (e) { toast(e.message, true); }
};
$("#logoutBtn").onclick = async () => {
  if (!confirm("Sign out of Claude on this PC?")) return;
  await api("/api/logout", { method: "POST" });
  loadStatus();
};
$("#refreshAuthBtn").onclick = async () => { const ok = await loadStatus(); toast(ok ? "Connected" : "Not signed in yet", !ok); };
$("#testBtn").onclick = async () => {
  const btn = $("#testBtn");
  busy(btn, true, "Testing");
  try {
    const r = await getJSON("/api/test", { method: "POST" });
    toast(r.ok ? `Claude replied: ${r.reply}` : `Failed: ${r.error}`, !r.ok);
  } catch (e) { toast(e.message, true); }
  finally { busy(btn, false); }
};
$("#saveKeyBtn").onclick = async () => {
  const key = $("#apiKey").value.trim();
  if (!key) return toast("Paste a key first.", true);
  await getJSON("/api/settings", { method: "POST", body: { api_key: key } });
  $("#apiKey").value = "";
  toast("API key saved");
  loadSettings();
};
$("#clearKeyBtn").onclick = async () => {
  await getJSON("/api/settings", { method: "POST", body: { api_key: "" } });
  toast("API key removed");
  loadSettings();
};
$$("#modelSel button").forEach((b) => b.addEventListener("click", async () => {
  await getJSON("/api/settings", { method: "POST", body: { model: b.dataset.model } });
  $$("#modelSel button").forEach((x) => x.classList.toggle("active", x === b));
  toast(`Model: ${b.textContent}`);
}));

// ---------- boot ----------
(async function boot() {
  initTheme();
  const ok = await loadStatus();
  try {
    const s = await getJSON("/api/settings");
    renderRecent(s.repos);
    if (s.last_repo) { $("#repoPath").value = s.last_repo; openRepo(); }
  } catch (e) {}
  if (!ok) { show("settings"); toast("Connect your Claude account to get started."); }
})();
