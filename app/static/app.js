// ReviewKit UI
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const state = {
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

// ---------- confirmation dialog ----------
// ask({ title, message, details, ok, alt, danger }) -> "ok" | "alt" | false.
// Cancel has focus by default, and Esc / clicking outside cancels, so a stray Enter or double-click never confirms.
let dialogOpen = null;
function ask({ title, message = "", details = [], ok = "Confirm", alt = "", danger = false }) {
  if (dialogOpen) return Promise.resolve(false);  // one dialog at a time; ignore repeat clicks
  const d = $("#confirmDialog");
  $("#cdTitle").textContent = title;
  $("#cdMessage").textContent = message;
  const dl = $("#cdDetails");
  dl.innerHTML = "";
  details.filter(([, v]) => v).forEach(([k, v]) => {
    const dt = document.createElement("dt"); dt.textContent = k;
    const dd = document.createElement("dd"); dd.textContent = v;
    dl.append(dt, dd);
  });
  dl.classList.toggle("hidden", !dl.children.length);
  const okBtn = $("#cdOk"), altBtn = $("#cdAlt"), cancel = $("#cdCancel");
  okBtn.textContent = ok;
  okBtn.className = "btn " + (danger ? "danger-solid" : "primary");
  altBtn.textContent = alt;
  altBtn.classList.toggle("hidden", !alt);
  d.classList.remove("hidden");
  setTimeout(() => cancel.focus(), 0);
  dialogOpen = new Promise((resolve) => {
    const done = (v) => {
      d.classList.add("hidden");
      document.removeEventListener("keydown", onKey, true);
      dialogOpen = null;
      resolve(v);
    };
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); done(false); } };
    document.addEventListener("keydown", onKey, true);
    okBtn.onclick = () => done("ok");
    altBtn.onclick = () => done("alt");
    cancel.onclick = () => done(false);
    d.onclick = (e) => { if (e.target === d) done(false); };
  });
  return dialogOpen;
}

// Ignore clicks while the previous click on the same button is still being handled
// (checking, waiting for confirmation, saving), so a double-click can't run an action twice.
function guarded(fn) {
  let pending = false;
  return async (...args) => {
    if (pending) return;
    pending = true;
    try { return await fn(...args); } finally { pending = false; }
  };
}

// Has this exact review (same title) already been run recently? Returns the newest match.
async function previousReview(title) {
  try { return (await getJSON("/api/history")).find((e) => e.title === title) || null; }
  catch (e) { return null; }
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
  if (state.status && !state.status.git && !state.gitWarned) {
    state.gitWarned = true;
    toast("Git was not found. Install Git for Windows to run code reviews.", true);
  }
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
    renderStack(info.stack);
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
// Review type is detected from the repository (not chosen by hand), so the wrong rules can't be picked.
function renderStack(stack) {
  const box = $("#reviewType");
  const ok = !!(stack && stack.types && stack.types.length);
  box.classList.toggle("bad", !!stack && !ok);
  $("#reviewTypeLabel").textContent = !stack ? "Open a repository" : ok ? stack.label : "No supported project found";
  $("#reviewTypeDetail").textContent = !stack ? "Chosen automatically from the repository's projects."
    : ok ? stack.detail
    : "ReviewKit reviews .NET, Angular and Ionic projects, but found no .sln/.csproj, angular.json or ionic.config.json in this repository.";
  $("#runCodeBtn").disabled = !!stack && !ok;
}

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
  stop.onclick = guarded(async () => {
    const yes = await ask({
      title: "Stop this review?",
      message: "Claude stops now. The partial result isn't saved to History, and you'd have to run the review again from the start.",
      ok: "Stop review", danger: true,
    });
    if (yes && !controller.signal.aborted) controller.abort();
  });
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
        if (ev.t === "info") {
          const counts = Object.entries(ev.counts || {}).filter(([, n]) => n).map(([k, n]) => `${k}: ${n} file${n > 1 ? "s" : ""}`);
          sub = sub.replace(/ · [^·]*$/, "") + ` · ${ev.label}`;
          p.setSub(sub);
          now.textContent = `Review rules: ${ev.label}${counts.length > 1 ? ` (${counts.join(", ")})` : ""}`;
        }
        else if (ev.t === "reset") { live = ""; }
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

$("#runCodeBtn").onclick = guarded(async () => {
  if (!state.repo) return toast("Open a local repository first.", true);
  const source = $("#sourceBranch").value.trim(), target = $("#targetBranch").value.trim();
  if (!source || !target) return toast("Enter both a source and a target branch.", true);
  if (source === target) return toast("Source and target must be different branches.", true);
  if (state.running.code) return toast("A code review is already running.", true);
  // Ask the server which rules this exact change gets (also catches bad branch names before confirming).
  const runBtn = $("#runCodeBtn");
  let rules;
  busy(runBtn, true, "Checking");
  try { rules = await getJSON("/api/review/code/rules", { method: "POST", body: { repo: state.repo.root, source, target } }); }
  catch (e) { return toast(e.message, true); }
  finally { busy(runBtn, false); }
  if (!rules.types.length) return toast("No supported project found in this repository.", true);
  const counts = Object.entries(rules.counts).filter(([, n]) => n).map(([k, n]) => `${k}: ${n}`);
  const label = rules.label;
  const title = `${state.repo.name}: ${source} → ${target}`;
  const prev = await previousReview(title);
  const choice = await ask({
    title: prev ? "This branch was already reviewed" : "Run code review?",
    message: prev
      ? `${source} → ${target} was reviewed on ${prev.created}. Running it again starts a new review and uses more of your Claude usage.`
      : "Claude reviews every change between these branches. This can take a few minutes.",
    details: [["Repository", state.repo.name], ["Source → target", `${source} → ${target}`],
              ["Review rules", label + (counts.length > 1 ? ` (${counts.join(", ")})` : "")],
              ["Changed files", String(rules.files)],
              ["Notes", $("#codeNotes").value.trim()]],
    ok: prev ? "Run again" : "Run review",
    alt: prev ? "Open previous review" : "",
  });
  if (choice === "alt") return openHistoryEntry(prev.id);
  if (choice !== "ok") return;
  streamReview("code", `${source} → ${target}`, `${state.repo.name} · ${label}`, {
    url: "/api/review/code",
    body: { repo: state.repo.root, source, target, notes: $("#codeNotes").value },
  }, $("#runCodeBtn"));
});

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

$("#runTdBtn").onclick = guarded(async () => {
  if (!state.docx) return;
  if (state.running.td) return toast("A technical design review is already running.", true);
  const prev = await previousReview(state.docx.name);
  const choice = await ask({
    title: prev ? "This document was already reviewed" : "Run technical design review?",
    message: prev
      ? `${state.docx.name} was reviewed on ${prev.created}. Running it again starts a new review and uses more of your Claude usage.`
      : "Claude reviews the whole document and returns prioritized review points.",
    details: [["Document", state.docx.name], ["Notes", $("#tdNotes").value.trim()]],
    ok: prev ? "Run again" : "Run review",
    alt: prev ? "Open previous review" : "",
  });
  if (choice === "alt") return openHistoryEntry(prev.id);
  if (choice !== "ok") return;
  const fd = new FormData();
  fd.append("file", state.docx);
  fd.append("notes", $("#tdNotes").value);
  streamReview("td", state.docx.name, "Technical Design", { url: "/api/review/td", body: fd }, $("#runTdBtn"));
});

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
  if (!box) { box = document.createElement("div"); box.className = "apply"; $(".r-scroll", panel.node).appendChild(box); }
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
  $(".a-no", box).onclick = async () => {
    if (!await ask({ title: "Leave the document as is?", message: "You can still apply the review points later from History.", ok: "Leave as is" })) return;
    box.innerHTML = `<div class="apply-note muted small">OK, the document was left as is. You can apply points later from History.</div>`; };
  $(".a-yes", box).onclick = () => renderApplySelect(panel, box, entry, points);
}

// back: { label, go } — where the Back/Cancel button returns to (the offer, or the applied results).
function renderApplySelect(panel, box, entry, points, back) {
  back = back || { label: "Cancel", go: () => attachApply(panel, { ...entry, points: points.map((p) => ({ ...p, status: "open" })) }) };
  // Changing an earlier selection starts from what was applied last time.
  const wasTicked = (p) => p.status !== "not_applicable";
  const sorted = [...points].sort((a, b) => (SEV_ORDER[(a.severity || "").toLowerCase()] ?? 9) - (SEV_ORDER[(b.severity || "").toLowerCase()] ?? 9));
  box.innerHTML = `
    <div class="apply-head">
      <div><b>Choose the review points to apply</b>
        <div class="muted small">Untick anything that doesn't apply. Unticked points are marked <b>Not applicable</b>, and their parts of the document aren't touched.</div></div>
      <div class="row"><button class="btn tiny ghost a-all">Select all</button><button class="btn tiny ghost a-none">Select none</button></div>
    </div>
    <div class="point-list">${sorted.map((p) => `
      <label class="point">
        <input type="checkbox" value="${escapeHtml(p.id)}"${wasTicked(p) ? " checked" : ""}>
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
      <div class="row"><button class="btn ghost a-cancel"></button><button class="btn primary a-go"></button></div>
    </div>`;
  const boxes = $$("input[type=checkbox]", box);
  const initial = boxes.map((b) => b.checked).join();
  $(".a-cancel", box).textContent = back.label;
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
  $(".a-cancel", box).onclick = guarded(async () => {
    const changed = boxes.map((b) => b.checked).join() !== initial;
    if (changed && !await ask({ title: "Discard your changes?", message: "The points you ticked or unticked here are reset. Nothing is applied to the document.", ok: "Discard changes" })) return;
    back.go();
  });
  $(".a-go", box).onclick = async () => {
    const selected = boxes.filter((b) => b.checked).map((b) => b.value);
    const already = !!entry.reviewed_name;
    const yes = await ask({
      title: `Apply ${selected.length} review point${selected.length > 1 ? "s" : ""}?`,
      message: (already ? "This replaces the edited copy made earlier. Changes you made to that copy outside ReviewKit are lost unless you saved it elsewhere (Save a copy…). " : "") +
        "Claude writes the edits as tracked changes into a copy of the document. Your original file isn't changed.",
      details: [["Applying", selected.join(", ")], ["Not applicable", `${boxes.length - selected.length} point(s)`]],
      ok: already ? "Replace and apply" : "Apply points",
    });
    if (yes) runApply(panel, box, entry, selected, back);
  };
  sync();
}

async function runApply(panel, box, entry, selected, back) {
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
    box.innerHTML = `<div class="error-box"></div><div class="row" style="margin-top:10px"><button class="btn ghost a-back"></button><button class="btn secondary a-retry">Back to selection</button></div>`;
    $(".error-box", box).textContent = e.message;
    const b = $(".a-back", box);
    b.textContent = back && back.label !== "Cancel" ? back.label : "Close";
    b.onclick = () => (back ? back.go() : attachApply(panel, entry));
    $(".a-retry", box).onclick = () => renderApplySelect(panel, box, entry, entry.points, back);
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
  $(".a-change", box).onclick = () => renderApplySelect(panel, box, entry, points, {
    label: "Back to results",
    go: () => renderApplyResult(panel, box, entry, points),
  });
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
    b.onclick = () => openHistoryEntry(e.id);
    list.appendChild(b);
  });
}
async function openHistoryEntry(id) {
  state.historyId = id;
  if (!$("#view-history").classList.contains("active")) show("history"); else renderHistory();
  let full;
  try { full = await getJSON(`/api/history/${id}`); } catch (err) { return toast(err.message, true); }
  const p = showStatic("history", full);
  const del = document.createElement("button");
  del.className = "btn tiny ghost danger";
  del.textContent = "Delete";
  del.onclick = async () => {
    if (!await ask({ title: "Delete this review?", message: "It's removed from History on this PC. This can't be undone.", details: [["Review", full.title], ["Created", full.created]], ok: "Delete", danger: true })) return;
    await api(`/api/history/${id}`, { method: "DELETE" });
    state.historyId = null;
    $('[data-slot="history"]').innerHTML = "";
    loadHistory();
  };
  $(".result-head .row", p.node).appendChild(del);
}
async function loadHistory() {
  try { historyItems = await getJSON("/api/history"); } catch (e) { historyItems = []; }
  renderHistory();
}
$("#historySearch").addEventListener("input", renderHistory);

// ---------- settings ----------
async function loadSettings() {
  await loadStatus();
  const s = await getJSON("/api/settings");
  $("#keyHint").textContent = s.api_key_set ? `A key ending in ${s.api_key_hint} is saved.` : "No API key saved.";
  $$("#modelSel button").forEach((b) => b.classList.toggle("active", b.dataset.model === (s.model || "")));
  renderArchive(s);
  $("#autoUpdate").checked = s.auto_update !== false;
  if (st) $("#curVersion").textContent = st.version;
  const st = state.status;
  if (st) {
    $("#about").innerHTML = st.reviews.map((r) => `<div class="skill"><b></b><span class="muted small"></span></div>`).join("") +
      `<div class="skill"><b>ReviewKit</b><span class="muted small">Version ${st.version}</span></div>`;
    $$("#about .skill").forEach((el, i) => {
      if (!st.reviews[i]) return;
      $("b", el).textContent = st.reviews[i].label;
      $("span", el).textContent = st.reviews[i].kind === "td" ? "Embedded review rules · documents" : "Embedded review rules · code";
    });
  }
}

$("#loginBtn").onclick = async () => {
  const btn = $("#loginBtn");
  busy(btn, true, "Waiting for sign-in");
  try {
    await api("/api/login", { method: "POST" });
    toast("Finish signing in in your browser. ReviewKit updates automatically.");
    // Long-poll the server (timers are throttled while this window is in the background,
    // but a pending request still resolves). Gives up after ~10 minutes.
    for (let i = 0; i < 24; i++) {
      const r = await getJSON("/api/login/wait");
      if (r.auth && r.auth.loggedIn) { await loadStatus(); if (state.status) loadSettings(); toast("Connected to Claude"); return; }
      if (!r.running) { const ok = await loadStatus(); if (!ok) toast("Sign-in window closed before finishing.", true); return; }
    }
  } catch (e) { toast(e.message, true); }
  finally { busy(btn, false); }
};
// Coming back to the window (e.g. from the browser after signing in) refreshes the account.
let lastFocusCheck = 0;
function refreshOnReturn() {
  if (document.visibilityState === "hidden" || Date.now() - lastFocusCheck < 4000) return;
  lastFocusCheck = Date.now();
  const known = !!state.status;
  const wasOk = !!(known && state.status.auth && state.status.auth.loggedIn);
  loadStatus().then((ok) => { if (ok && known && !wasOk) toast("Connected to Claude"); });
}
window.addEventListener("focus", refreshOnReturn);
document.addEventListener("visibilitychange", refreshOnReturn);
$("#logoutBtn").onclick = guarded(async () => {
  const running = [state.running.code && "a code review", state.running.td && "a technical design review"].filter(Boolean);
  if (!await ask({
    title: running.length ? "A review is still running" : "Sign out of Claude?",
    message: (running.length
      ? `Signing out now stops ${running.join(" and ")}. ${running.length > 1 ? "Their results aren't" : "Its result isn't"} saved and you'd have to run ${running.length > 1 ? "them" : "it"} again. `
      : "") + "This also signs out Claude Code on this PC, because they share the same sign-in.",
    ok: running.length ? "Stop and sign out" : "Sign out", danger: true,
  })) return;
  // Stop running reviews first so they end cleanly as "Stopped" rather than failing mid-stream.
  Object.values(state.running).forEach((c) => c && c.abort());
  await api("/api/logout", { method: "POST" });
  loadStatus();
});
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
$("#saveKeyBtn").onclick = guarded(async () => {
  const key = $("#apiKey").value.trim();
  if (!key) return toast("Paste a key first.", true);
  if (!await ask({ title: "Save this API key?", message: "Reviews will use this key (and its billing) instead of your Claude sign-in.", details: [["Key", `…${key.slice(-4)}`]], ok: "Save key" })) return;
  await getJSON("/api/settings", { method: "POST", body: { api_key: key } });
  $("#apiKey").value = "";
  toast("API key saved");
  loadSettings();
});
$("#clearKeyBtn").onclick = guarded(async () => {
  if (!await ask({ title: "Remove the saved API key?", message: "Reviews will use your Claude sign-in instead.", ok: "Remove key", danger: true })) return;
  await getJSON("/api/settings", { method: "POST", body: { api_key: "" } });
  toast("API key removed");
  loadSettings();
});
$$("#modelSel button").forEach((b) => b.addEventListener("click", async () => {
  await getJSON("/api/settings", { method: "POST", body: { model: b.dataset.model } });
  $$("#modelSel button").forEach((x) => x.classList.toggle("active", x === b));
  toast(`Model: ${b.textContent}`);
}));

// ---------- history retention / archive ----------
function renderArchive(s) {
  $("#retentionDays").textContent = s.retention_days || 7;
  $("#historyNote").textContent = `Reviews are saved on this PC for ${s.retention_days || 7} days` +
    (s.auto_archive && s.archive_dir ? `, then archived to ${s.archive_dir}.` : ".");
  $("#autoArchive").checked = !!s.auto_archive;
  $("#archiveDir").value = s.archive_dir || "";
  $("#archiveDir").disabled = $("#archiveBrowse").disabled = !s.auto_archive;
  const hint = $("#archiveHint"), p = s.purge || {};
  hint.classList.toggle("warn-text", !!p.error);
  hint.textContent = p.error ? p.error
    : s.auto_archive ? (s.archive_dir ? `Old reviews are moved to ${s.archive_dir}, one folder per month.` : "Choose a folder, then click Save.")
    : "Auto-archive is off: reviews older than 7 days are deleted.";
}
async function saveArchive(body) {
  try {
    renderArchive(await getJSON("/api/settings", { method: "POST", body }));
    toast(body.auto_archive === false ? "Auto-archive turned off" : "Archive settings saved");
  } catch (e) {
    toast(e.message, true);
    try { renderArchive(await getJSON("/api/settings")); } catch (_) {}
  }
}
$("#autoArchive").addEventListener("change", (e) => {
  const on = e.target.checked;
  $("#archiveDir").disabled = $("#archiveBrowse").disabled = !on;
  if (!on) {
    ask({ title: "Turn off auto-archive?", message: "Reviews older than 7 days will be deleted instead of archived.", ok: "Turn off", danger: true })
      .then((yes) => { if (yes) saveArchive({ auto_archive: false }); else { e.target.checked = true; $("#archiveDir").disabled = $("#archiveBrowse").disabled = false; } });
  }
  else if ($("#archiveDir").value.trim()) saveArchive({ auto_archive: true, archive_dir: $("#archiveDir").value });
  else { $("#archiveHint").textContent = "Choose a folder, then click Save."; $("#archiveDir").focus(); }
});
$("#archiveSave").onclick = guarded(async () => {
  if (!$("#autoArchive").checked) return toast("Turn on auto-archive first.", true);
  if (!await ask({ title: "Save archive folder?", message: "Reviews older than 7 days are moved there now and from now on.", details: [["Folder", $("#archiveDir").value.trim()]], ok: "Save" })) return;
  saveArchive({ auto_archive: true, archive_dir: $("#archiveDir").value });
});
$("#archiveBrowse").onclick = async () => {
  try {
    const r = await getJSON("/api/pick-folder", { method: "POST" });
    if (r.path) { $("#archiveDir").value = r.path; saveArchive({ auto_archive: true, archive_dir: r.path }); }
  } catch (e) { toast(e.message, true); }
};

// ---------- updates ----------
let updateInfo = null;
const reviewRunning = () => !!(state.running.code || state.running.td);

async function checkUpdate({ boot = false, manual = false } = {}) {
  let u;
  try { u = await getJSON("/api/update"); } catch (e) { u = { error: e.message }; }
  updateInfo = u;
  const line = $("#updateStatus");
  if (u.error) {
    line.innerHTML = `<span class="dot bad"></span><span></span>`;
    $("span:last-child", line).textContent = u.error;
    if (manual) toast(u.error, true);
    return;
  }
  line.innerHTML = `<span class="dot ${u.available ? "bad" : "ok"}"></span><span></span><span class="muted"></span>`;
  $$("span", line)[1].textContent = u.available ? `Version ${u.latest} is available` : `You're up to date · ${u.current}`;
  $$("span", line)[2].textContent = u.available ? `installed: ${u.current}` : "";
  if (!u.available) { $("#updateBar").classList.add("hidden"); if (manual) toast("ReviewKit is up to date"); return; }

  let autoUpdate = true;
  try { autoUpdate = (await getJSON("/api/settings")).auto_update !== false; } catch (e) {}
  if (boot && autoUpdate && u.can_install && !reviewRunning()) return installUpdate();
  $("#updateText").textContent = `ReviewKit ${u.latest} is available (you have ${u.current}).` +
    (u.can_install ? "" : ` ${u.reason || ""}`);
  $("#updateNow").classList.toggle("hidden", !u.can_install);
  $("#updateBar").classList.remove("hidden");
}

async function installUpdate() {
  if (reviewRunning()) return toast("Wait for the running review to finish, then update.", true);
  const ov = $("#updateOverlay"), status = $("#ovStatus"), bar = $("#ovBar");
  $("#ovTitle").textContent = `Updating ReviewKit${updateInfo && updateInfo.latest ? ` to ${updateInfo.latest}` : ""}…`;
  $("#ovClose").classList.add("hidden");
  bar.style.width = "0";
  ov.classList.remove("hidden");
  try {
    const res = await api("/api/update/install", { method: "POST" });
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
        if (ev.t === "status") status.textContent = ev.v;
        else if (ev.t === "progress") {
          bar.style.width = `${ev.v}%`;
          status.textContent = ev.total ? `Downloading · ${(ev.done / 1048576).toFixed(0)} of ${(ev.total / 1048576).toFixed(0)} MB` : "Downloading…";
        } else if (ev.t === "error") throw new Error(ev.v);
        else if (ev.t === "done") {
          bar.style.width = "100%";
          status.textContent = "Restarting ReviewKit…";
          return;
        }
      }
    }
    throw new Error("The update stopped before it finished.");
  } catch (e) {
    status.textContent = e.message;
    $("#ovTitle").textContent = "Update failed";
    const close = $("#ovClose");
    close.classList.remove("hidden");
    close.onclick = () => ov.classList.add("hidden");
  }
}
$("#updateNow").onclick = guarded(async () => {
  if (!await ask({ title: "Update ReviewKit now?", message: "ReviewKit downloads the update, closes, and restarts on the new version.", details: [["New version", updateInfo && updateInfo.latest]], ok: "Update and restart" })) return;
  $("#updateBar").classList.add("hidden");
  installUpdate();
});
$("#updateLater").onclick = () => $("#updateBar").classList.add("hidden");
$("#checkUpdateBtn").onclick = async () => {
  const btn = $("#checkUpdateBtn");
  busy(btn, true, "Checking");
  try { await checkUpdate({ manual: true }); } finally { busy(btn, false); }
};
$("#autoUpdate").addEventListener("change", async (e) => {
  if (!e.target.checked && !await ask({ title: "Turn off automatic updates?", message: "You'll need to click Update now yourself when a new version is out.", ok: "Turn off" })) { e.target.checked = true; return; }
  try { await getJSON("/api/settings", { method: "POST", body: { auto_update: e.target.checked } }); toast(e.target.checked ? "Automatic updates on" : "Automatic updates off"); }
  catch (err) { toast(err.message, true); }
});

// ---------- boot ----------
(async function boot() {
  initTheme();
  const ok = await loadStatus();
  try {
    const s = await getJSON("/api/settings");
    renderRecent(s.repos);
    renderArchive(s);
    if (s.last_repo) { $("#repoPath").value = s.last_repo; openRepo(); }
  } catch (e) {}
  if (!ok) { show("settings"); toast("Connect your Claude account to get started."); }
  checkUpdate({ boot: true });
  setInterval(() => checkUpdate({}), 6 * 3600 * 1000);
})();
