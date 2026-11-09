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
  const paint = () => { frame = 0; p.body.innerHTML = renderMarkdown(live); p.body.lastElementChild && p.body.lastElementChild.classList.add("cursor"); };

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
  try { await api("/api/login", { method: "POST" }); toast("Finish signing in in the console window, then click Refresh."); }
  catch (e) { toast(e.message, true); }
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
