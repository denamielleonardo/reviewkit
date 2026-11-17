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

// ---------- dropdowns ----------
const CHEVRON = `<svg viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>`;
const escRe = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
const highlight = (text, q) => q ? escapeHtml(text).replace(new RegExp(`(${escRe(escapeHtml(q))})`, "ig"), "<mark>$1</mark>") : escapeHtml(text);

// Dropdown lists float above the page (fixed position), so a scrolling card can't clip them. They sit
// under the field, or above it when there isn't room below, and follow the field while it scrolls.
function placePanel(panel, anchor, minWidth = 0) {
  const r = anchor.getBoundingClientRect(), gap = 6, margin = 8;
  const width = Math.min(Math.max(r.width, minWidth), window.innerWidth - margin * 2);
  const left = Math.min(Math.max(r.left, margin), window.innerWidth - width - margin);
  const below = window.innerHeight - r.bottom - gap - margin, above = r.top - gap - margin;
  const up = below < 220 && above > below;
  Object.assign(panel.style, {
    position: "fixed", left: `${left}px`, width: `${width}px`, right: "auto",
    top: up ? "auto" : `${r.bottom + gap}px`, bottom: up ? `${window.innerHeight - r.top + gap}px` : "auto",
    maxHeight: `${Math.max(Math.min(up ? above : below, 340), 140)}px`,
  });
}
function followAnchor(panel, anchor, minWidth) {
  const place = () => { if (!panel.classList.contains("hidden")) placePanel(panel, anchor, minWidth); };
  window.addEventListener("resize", place);
  document.addEventListener("scroll", (e) => { if (!panel.contains(e.target)) place(); }, true);
  return place;
}

// Closes a dropdown when the user clicks elsewhere; the listener goes away with the dropdown.
function onOutside(root, close) {
  const h = (e) => {
    if (!root.isConnected) return document.removeEventListener("mousedown", h, true);
    if (!root.contains(e.target)) close();
  };
  document.addEventListener("mousedown", h, true);
}

// Searchable single-choice dropdown on an existing <input>. The input keeps the value, so anything
// may still be typed; getItems() supplies the choices (e.g. the repository's branches).
function combo(input, getItems, { empty = "No matches" } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "combo";
  input.parentNode.insertBefore(wrap, input);
  wrap.appendChild(input);
  wrap.insertAdjacentHTML("beforeend",
    `<button type="button" class="combo-toggle" tabindex="-1" aria-label="Show choices">${CHEVRON}</button><div class="dd-panel hidden" role="listbox"></div>`);
  const panel = $(".dd-panel", wrap), toggle = $(".combo-toggle", wrap);
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("list");
  let active = -1, shown = [], filterOn = false;
  const MAX = 150;

  const render = () => {
    const q = filterOn ? input.value.trim().toLowerCase() : "";
    const all = getItems() || [];
    const hits = q ? all.filter((b) => b.toLowerCase().includes(q)) : all;
    shown = hits.slice(0, MAX);
    active = Math.min(active, shown.length - 1);
    panel.innerHTML = shown.length
      ? shown.map((b, i) => `<div class="dd-opt${i === active ? " active" : ""}${b === input.value ? " current" : ""}" role="option" data-i="${i}">${highlight(b, q)}</div>`).join("")
        + (hits.length > MAX ? `<div class="dd-meta">Showing ${MAX} of ${hits.length}. Type to narrow down.</div>` : "")
      : `<div class="dd-empty">${all.length ? empty : "No branches yet. Open a repository or click Fetch."}</div>`;
  };
  const place = followAnchor(panel, input, 340);
  const open = () => { if (!panel.classList.contains("hidden")) return; wrap.classList.add("open"); panel.classList.remove("hidden"); input.setAttribute("aria-expanded", "true"); render(); place(); };
  const close = () => { wrap.classList.remove("open"); panel.classList.add("hidden"); input.setAttribute("aria-expanded", "false"); active = -1; filterOn = false; };
  const pick = (i) => {
    if (shown[i] === undefined) return;
    input.value = shown[i];
    input.dispatchEvent(new Event("change", { bubbles: true }));
    close();
  };
  const move = (d) => {
    if (!shown.length) return;
    active = (active + d + shown.length) % shown.length;
    render();
    const el = panel.querySelector(".dd-opt.active");
    if (el) el.scrollIntoView({ block: "nearest" });
  };
  input.addEventListener("focus", open);
  input.addEventListener("click", open);
  input.addEventListener("input", () => { filterOn = true; active = shown.length ? 0 : -1; open(); render(); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); open(); move(1); }
    else if (e.key === "ArrowUp") { e.preventDefault(); open(); move(-1); }
    else if (e.key === "Enter" && !panel.classList.contains("hidden") && active >= 0) { e.preventDefault(); pick(active); }
    else if (e.key === "Escape" || e.key === "Tab") close();
  });
  toggle.addEventListener("mousedown", (e) => { e.preventDefault(); if (panel.classList.contains("hidden")) { input.focus(); open(); } else close(); });
  panel.addEventListener("mousedown", (e) => { e.preventDefault(); const o = e.target.closest(".dd-opt"); if (o) pick(+o.dataset.i); });
  onOutside(wrap, close);
  return { refresh: () => { if (!panel.classList.contains("hidden")) render(); } };
}

// Multi-choice people picker (Required reviewers, Send to): chips for the chosen people, a searchable
// list with checkboxes, and "Other…" to add someone, who is then kept in the list on this PC.
// people: [{email, name, custom}]; selected: [email]. Returns { values(), setPeople(list) }.
function peoplePicker(root, { people, selected, placeholder = "Select people", onChange = () => {} }) {
  let list = people.slice();
  let chosen = [...new Set(selected)];
  let q = "", active = -1;
  root.classList.add("ms");
  root.innerHTML = `
    <div class="ms-control" tabindex="0" role="combobox" aria-expanded="false"><div class="ms-chips"></div><span class="ms-chev">${CHEVRON}</span></div>
    <div class="dd-panel ms-panel hidden">
      <input class="ms-search" placeholder="Search by name or email" autocomplete="off" spellcheck="false">
      <div class="ms-list" role="listbox" aria-multiselectable="true"></div>
      <div class="ms-other">
        <button type="button" class="ms-other-btn">+ Other…</button>
        <div class="ms-other-form hidden">
          <input class="ms-o-email" placeholder="Email address" autocomplete="off" spellcheck="false">
          <input class="ms-o-name" placeholder="Name (optional)" autocomplete="off">
          <div class="row"><button type="button" class="btn tiny ghost ms-o-cancel">Cancel</button><button type="button" class="btn tiny primary ms-o-add">Add</button></div>
        </div>
      </div>
    </div>`;
  const control = $(".ms-control", root), chips = $(".ms-chips", root), panel = $(".ms-panel", root);
  const search = $(".ms-search", root), box = $(".ms-list", root);
  const person = (email) => list.find((p) => p.email.toLowerCase() === email.toLowerCase()) || { email, name: "" };
  const label = (p) => p.name || p.email;

  const renderChips = () => {
    chips.innerHTML = chosen.length
      ? chosen.map((e) => `<span class="ms-chip" title="${escapeHtml(e)}">${escapeHtml(label(person(e)))}<button type="button" data-e="${escapeHtml(e)}" aria-label="Remove ${escapeHtml(label(person(e)))}">×</button></span>`).join("")
      : `<span class="ms-placeholder">${escapeHtml(placeholder)}</span>`;
  };
  const visible = () => {
    // People chosen earlier who aren't in the list (e.g. typed in an older version) are still shown.
    const extra = chosen.filter((e) => !list.some((p) => p.email.toLowerCase() === e.toLowerCase())).map((e) => ({ email: e, name: "" }));
    const all = [...list, ...extra];
    const t = q.trim().toLowerCase();
    return t ? all.filter((p) => p.email.toLowerCase().includes(t) || p.name.toLowerCase().includes(t)) : all;
  };
  const renderList = () => {
    const items = visible();
    active = Math.min(active, items.length - 1);
    box.innerHTML = items.length ? items.map((p, i) => {
      const on = chosen.some((e) => e.toLowerCase() === p.email.toLowerCase());
      return `<div class="dd-opt${i === active ? " active" : ""}" role="option" aria-selected="${on}" data-e="${escapeHtml(p.email)}">
        <span class="ms-check"><svg viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></svg></span>
        <span class="who"><b>${highlight(p.name || p.email, q.trim())}</b>${p.name ? `<span>${highlight(p.email, q.trim())}</span>` : ""}</span>
        ${p.custom ? `<button type="button" class="dd-del" data-del="${escapeHtml(p.email)}" title="Remove from this list" aria-label="Remove ${escapeHtml(label(p))} from the list">×</button>` : ""}
      </div>`;
    }).join("") : `<div class="dd-empty">No one matches. Use “Other…” to add them.</div>`;
  };
  const changed = () => { renderChips(); renderList(); onChange(chosen.slice()); };
  const toggle = (email) => {
    const i = chosen.findIndex((e) => e.toLowerCase() === email.toLowerCase());
    if (i >= 0) chosen.splice(i, 1); else chosen.push(email);
    changed();
  };
  const isOpen = () => !panel.classList.contains("hidden");
  const place = followAnchor(panel, control, 320);
  const open = () => { if (isOpen()) return; root.classList.add("open"); panel.classList.remove("hidden"); control.setAttribute("aria-expanded", "true"); q = ""; search.value = ""; renderList(); place(); setTimeout(() => search.focus(), 0); };
  const close = () => { root.classList.remove("open"); panel.classList.add("hidden"); control.setAttribute("aria-expanded", "false"); hideOther(); active = -1; };
  const showOther = () => { $(".ms-other-btn", root).classList.add("hidden"); $(".ms-other-form", root).classList.remove("hidden"); const e = $(".ms-o-email", root); e.value = search.value.includes("@") ? search.value.trim() : ""; e.focus(); };
  const hideOther = () => { $(".ms-other-btn", root).classList.remove("hidden"); $(".ms-other-form", root).classList.add("hidden"); $(".ms-o-email", root).value = ""; $(".ms-o-name", root).value = ""; };
  const addOther = guarded(async () => {
    const email = $(".ms-o-email", root).value.trim(), name = $(".ms-o-name", root).value.trim();
    if (!/^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$/.test(email)) { $(".ms-o-email", root).focus(); return toast("Enter an email address.", true); }
    try {
      const r = await getJSON("/api/pr/people", { method: "POST", body: { email, name } });
      peoplePicker.shared(r.people);
      if (!chosen.some((e) => e.toLowerCase() === email.toLowerCase())) chosen.push(email);
      hideOther(); q = ""; search.value = ""; changed(); search.focus();
      toast(`${name || email} added to the list`);
    } catch (e) { toast(e.message, true); }
  });

  control.addEventListener("click", (e) => {
    const x = e.target.closest(".ms-chip button");
    if (x) { e.stopPropagation(); toggle(x.dataset.e); return; }
    isOpen() ? close() : open();
  });
  control.addEventListener("keydown", (e) => { if (["Enter", " ", "ArrowDown"].includes(e.key)) { e.preventDefault(); open(); } });
  search.addEventListener("input", () => { q = search.value; active = 0; renderList(); });
  search.addEventListener("keydown", (e) => {
    const items = visible();
    if (e.key === "ArrowDown") { e.preventDefault(); active = (active + 1) % Math.max(items.length, 1); renderList(); }
    else if (e.key === "ArrowUp") { e.preventDefault(); active = (active - 1 + items.length) % Math.max(items.length, 1); renderList(); }
    else if (e.key === "Enter") { e.preventDefault(); if (items[active]) toggle(items[active].email); else if (!items.length) showOther(); }
    else if (e.key === "Escape") { e.preventDefault(); close(); control.focus(); }
    const el = box.querySelector(".dd-opt.active"); if (el) el.scrollIntoView({ block: "nearest" });
  });
  box.addEventListener("click", guarded(async (e) => {
    const del = e.target.closest(".dd-del");
    if (del) {
      e.stopPropagation();
      const email = del.dataset.del;
      try { peoplePicker.shared((await getJSON("/api/pr/people/remove", { method: "POST", body: { email } })).people); }
      catch (err) { return toast(err.message, true); }
      chosen = chosen.filter((x) => x.toLowerCase() !== email.toLowerCase());
      return changed();
    }
    const o = e.target.closest(".dd-opt");
    if (o) toggle(o.dataset.e);
  }));
  $(".ms-other-btn", root).onclick = showOther;
  $(".ms-o-cancel", root).onclick = () => { hideOther(); search.focus(); };
  $(".ms-o-add", root).onclick = addOther;
  [".ms-o-email", ".ms-o-name"].forEach((s) => $(s, root).addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); addOther(); } if (e.key === "Escape") { e.preventDefault(); hideOther(); search.focus(); } }));
  onOutside(root, close);
  renderChips();

  const api = { root, values: () => chosen.slice(), setPeople: (p) => { list = p.slice(); renderChips(); if (isOpen()) renderList(); } };
  peoplePicker.instances.add(api);
  return api;
}
// Every picker shows the same list, so someone added in one appears in the others straight away.
peoplePicker.instances = new Set();
peoplePicker.shared = (people) => peoplePicker.instances.forEach((p) => {
  if (p.root.isConnected) p.setPeople(people); else peoplePicker.instances.delete(p);
});

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
  if (view === "admin") loadAdmin();
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
  renderActivity(state.status && state.status.activity);
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
    c.textContent = repoLabel(r.split(/[\\/]/).filter(Boolean).pop());
    c.onclick = () => { $("#repoPath").value = r; openRepo(); };
    box.appendChild(c);
  });
}

// Folders cloned from Azure DevOps can be named "My%20Repo"; show them as "My Repo".
function repoLabel(name) {
  try { return decodeURIComponent(name); } catch (e) { return name; }
}

state.branches = [];
const branchCombos = ["#sourceBranch", "#targetBranch"].map((s) => combo($(s), () => state.branches, { empty: "No branch matches. You can still type its full name." }));
function setBranches(list) {
  state.branches = list || [];
  branchCombos.forEach((c) => c.refresh());
}

async function openRepo() {
  const path = $("#repoPath").value.trim();
  if (!path) return toast("Browse to your local clone first.", true);
  const btn = $("#browseBtn");
  busy(btn, true, "Opening");
  try {
    const info = await getJSON("/api/repo", { method: "POST", body: { path } });
    state.repo = info;
    $("#repoPath").value = info.root;
    $("#repoName").textContent = repoLabel(info.name);
    toast(`Opened ${repoLabel(info.name)} · ${info.branches.length} branch${info.branches.length === 1 ? "" : "es"}`);
    $("#repoName").title = info.root;
    $("#fetchStatus").classList.add("hidden");
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

// Fetch is often done in about a second, so the result is spelled out next to the button
// (what changed, and when) instead of only flashing a spinner and a toast.
async function fetchRepo() {
  if (!state.repo) return;
  const btn = $("#fetchBtn"), status = $("#fetchStatus");
  const before = new Set(state.branches || []);
  busy(btn, true, "Fetching");
  status.classList.remove("hidden", "warn-text");
  status.textContent = "Fetching from origin…";
  const started = Date.now();
  try {
    const r = await getJSON("/api/repo/fetch", { method: "POST", body: { path: state.repo.root } });
    await new Promise((ok) => setTimeout(ok, Math.max(0, 600 - (Date.now() - started))));  // let the spinner register
    setBranches(r.branches);
    const added = r.branches.filter((b) => !before.has(b));
    const removed = [...before].filter((b) => !r.branches.includes(b));
    const time = new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
    const change = before.size === 0 ? `${r.branches.length} branch${r.branches.length === 1 ? "" : "es"}`
      : added.length || removed.length
        ? [added.length && `${added.length} new`, removed.length && `${removed.length} deleted`].filter(Boolean).join(", ") +
          ` · ${r.branches.length} branch${r.branches.length === 1 ? "" : "es"}`
        : `up to date · ${r.branches.length} branch${r.branches.length === 1 ? "" : "es"}`;
    status.textContent = `Fetched at ${time} · ${change}` + (added.length && added.length <= 3 ? ` (new: ${added.join(", ")})` : "");
    toast(`Fetched · ${change}`);
  } catch (e) {
    status.classList.add("warn-text");
    status.textContent = `Fetch failed: ${e.message}`;
    toast(e.message, true);
  } finally {
    busy(btn, false);
  }
}

// Repositories are opened with Browse (or a recent chip); Enter in the box also opens a pasted path.
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
    : "ReviewKit reviews .NET, Python, Angular, Ionic, React and React Native projects, but found no project file for any of them (.sln/.csproj, pyproject.toml/requirements.txt, angular.json, ionic.config.json, or a package.json that uses React) in this repository.";
  $("#runCodeBtn").disabled = !!stack && !ok;
  renderVersions(ok ? stack : null);
}

// Framework versions under the review type (read from the checked-out branch's project files).
function renderVersions(stack) {
  const box = $("#reviewTypeVersions");
  const rows = (stack && stack.versions) || [];
  box.classList.toggle("hidden", !rows.length);
  const dl = $("dl", box);
  dl.innerHTML = "";
  rows.forEach((r) => {
    const dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = r.label;
    dd.textContent = r.text;
    dl.append(dt, dd);
  });
  $(".versions-note", box).textContent = rows.length
    ? `Versions on ${stack.versions_branch}. Each review uses the versions on its source branch.` : "";
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
  attachPr(p, entry);
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
          attachPr(p, ev.entry);
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

// ---------- pull request (Azure DevOps) + "[For Code Review]" email (Outlook on the web) ----------
const PR_ICON = `<svg viewBox="0 0 24 24"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="18" r="2.5"/><path d="M6 8.5v7M18 15.5V11a3 3 0 00-3-3h-4M13 5.5L10.5 8 13 10.5"/></svg>`;

function prBox(panel) {
  let box = $(".pr-box", panel.node);
  if (!box) { box = document.createElement("div"); box.className = "apply pr-box"; $(".r-scroll", panel.node).appendChild(box); }
  return box;
}

function attachPr(panel, entry) {
  if (!entry || entry.kind !== "code") return;
  const box = prBox(panel);
  if (entry.pr) return renderPrDone(panel, box, entry);
  box.innerHTML = `
    <div class="apply-offer">
      <div class="apply-icon">${PR_ICON}</div>
      <div class="apply-text">
        <b>Ready to ask for a code review?</b>
        <span class="muted small">ReviewKit creates the pull request <b>${escapeHtml(entry.source)} → ${escapeHtml(entry.target)}</b> in Azure DevOps, then opens a “[For Code Review]” email in Outlook on the web for you to check and send.</span>
      </div>
      <div class="row"><button class="btn primary p-start">Create PR &amp; email</button></div>
    </div>`;
  const start = $(".p-start", box);
  start.onclick = guarded(async () => {
    busy(start, true, "Checking");
    let prep;
    try { prep = await getJSON(`/api/code/${entry.id}/pr`); }
    catch (e) { busy(start, false); return toast(e.message, true); }
    renderPrForm(panel, box, entry, prep);
  });
}

function prEmailPreview(prId, title, greeting, url) {
  const line = `Pull Request ${prId}: ${title}`;
  return {
    subject: `[For Code Review]: ${line}`,
    body: `Hi ${greeting.trim() || "there"},\n\nGood day!\n\nI am kindly requesting a Code Review for the following:\n${line}\n${url}\n\nThank you!`,
  };
}

function renderPrForm(panel, box, entry, prep) {
  const ex = prep.existing;
  box.innerHTML = `
    <div class="apply-head">
      <div><b>${ex ? `Pull Request ${ex.id} already exists` : "Create the pull request"}</b>
        <div class="muted small">${escapeHtml(prep.repo_name)} · ${escapeHtml(prep.source)} → ${escapeHtml(prep.target)}</div></div>
    </div>
    ${prep.note ? `<div class="pr-note small"></div>` : ""}
    ${ex ? `<div class="muted small pr-existing">ReviewKit uses the existing pull request: it adds any required reviewers below and opens the email.</div>` : ""}
    <div class="pr-form">
      <div class="field"><label for="prTitle-${entry.id}">Pull request title</label>
        <input id="prTitle-${entry.id}" class="p-title" maxlength="400" autocomplete="off"${ex ? " readonly" : ""}></div>
      ${ex ? "" : `<div class="field"><label for="prDesc-${entry.id}">Description <span class="muted">(Markdown)</span></label>
        <textarea id="prDesc-${entry.id}" class="p-desc" rows="5" maxlength="4000"></textarea></div>`}
      <div class="field"><label>Required reviewers <span class="muted">(optional)</span></label>
        <div class="p-rev"></div></div>
      <div class="pr-grid">
        <div class="field"><label>Send to</label>
          <div class="p-to"></div></div>
        <div class="field"><label for="prGreet-${entry.id}">Greeting</label>
          <input id="prGreet-${entry.id}" class="p-greet" placeholder="Sir" autocomplete="off"></div>
      </div>
      <div class="muted small">Required reviewers, Send to and Greeting are remembered on this PC for your next pull request in any repository. Use “Other…” in either list to add someone.</div>
      <div class="field"><label>Email</label>
        <div class="pr-preview"><div class="pr-subject"></div><pre class="pr-body"></pre>
          <div class="muted small">Outlook adds your signature below this.</div></div></div>
    </div>
    <div class="apply-foot">
      <span class="muted small">The email opens in your browser for you to check and send. ReviewKit never sends it.</span>
      <div class="row"><button class="btn ghost p-cancel">Cancel</button><button class="btn primary p-go"></button></div>
    </div>`;
  if (prep.note) $(".pr-note", box).textContent = prep.note;
  const title = $(".p-title", box), desc = $(".p-desc", box), greet = $(".p-greet", box);
  const split = (s) => (s || "").split(/[;,\n]/).map((x) => x.trim()).filter(Boolean);
  const rev = peoplePicker($(".p-rev", box), { people: prep.people || [], selected: split(prep.reviewers), placeholder: "Choose required reviewers" });
  const to = peoplePicker($(".p-to", box), { people: prep.people || [], selected: split(prep.to), placeholder: "Choose who gets the email" });
  title.value = ex ? ex.title : prep.title;
  if (desc) desc.value = prep.description || "";
  greet.value = prep.greeting || "";
  const go = $(".p-go", box);
  go.textContent = ex ? "Open email" : "Create PR & open email";
  const sync = () => {
    const m = prEmailPreview(ex ? ex.id : "<number>", title.value.trim() || "…", greet.value, ex ? ex.url : "<link to the pull request>");
    $(".pr-subject", box).textContent = `Subject: ${m.subject}`;
    $(".pr-body", box).textContent = m.body;
  };
  [title, greet].forEach((el) => el.addEventListener("input", sync));
  sync();
  $(".p-cancel", box).onclick = () => attachPr(panel, entry);
  go.onclick = guarded(async () => {
    const t = title.value.trim(), addrs = to.values(), reviewers = rev.values();
    if (!t) { title.focus(); return toast("Enter a pull request title.", true); }
    if (!addrs.length) { $(".p-to .ms-control", box).focus(); return toast("Choose who the email goes to.", true); }
    const badAddr = addrs.find((a) => !/^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$/.test(a));
    if (badAddr) { return toast(`“${badAddr}” isn't an email address. Remove it from Send to.`, true); }
    const names = (emails) => emails.map((e) => ((prep.people || []).find((p) => p.email.toLowerCase() === e.toLowerCase()) || {}).name || e).join(", ");
    const yes = await ask({
      title: ex ? "Open the review request email?" : "Create this pull request?",
      message: ex
        ? `Pull Request ${ex.id} already exists, so ReviewKit adds the required reviewers to it and opens the email. Nothing is sent until you click Send in Outlook.`
        : "The pull request is created in Azure DevOps right away, where your team can see it. Then the email opens in your browser. Nothing is sent until you click Send in Outlook.",
      details: [["Repository", prep.repo_name], ["Source → target", `${prep.source} → ${prep.target}`],
                ["Title", ex ? `${ex.id}: ${t}` : t], ["Required reviewers", names(reviewers)], ["Email to", names(addrs)],
                ["Greeting", greet.value.trim()]],
      ok: ex ? "Open email" : "Create PR & open email",
    });
    if (!yes) return;
    busy(go, true, ex ? "Opening" : "Creating");
    try {
      const r = await getJSON(`/api/code/${entry.id}/pr`, { method: "POST",
        body: { title: t, description: desc ? desc.value : "", to: addrs.join("; "), greeting: greet.value, reviewers: reviewers.join("; ") } });
      Object.assign(entry, r.entry || { pr: r.pr });
      renderPrDone(panel, box, entry);
      toast(r.pr.existing ? `Using Pull Request ${r.pr.id} · email opened` : `Pull Request ${r.pr.id} created · email opened`);
    } catch (e) {
      busy(go, false);
      toast(e.message, true);
    }
  });
}

function renderPrDone(panel, box, entry) {
  const pr = entry.pr;
  box.innerHTML = `
    <div class="apply-offer">
      <div class="apply-icon">${PR_ICON}</div>
      <div class="apply-text">
        <b class="pr-done-title"></b>
        <span class="muted small pr-done-sub"></span>
      </div>
      <div class="row wrap">
        <button class="btn ghost p-open">Open pull request</button>
        <button class="btn ghost p-mail">Open email again</button>
        <button class="btn secondary p-again">Create PR &amp; email again</button>
      </div>
    </div>`;
  $(".pr-done-title", box).textContent = `Pull Request ${pr.id}: ${pr.title}`;
  $(".pr-done-sub", box).textContent = `${pr.existing ? "Existing pull request" : "Created"} ${pr.created || ""}` +
    ((pr.reviewers || []).length ? ` · required reviewers: ${pr.reviewers.join(", ")}` : "") + ` · review request to ${pr.to}`;
  const open = (what) => guarded(async () => {
    try { await getJSON(`/api/code/${entry.id}/pr/open`, { method: "POST", body: { what } }); }
    catch (e) { toast(e.message, true); }
  });
  $(".p-open", box).onclick = open("pr");
  $(".p-mail", box).onclick = open("email");
  // Back to the form: an open PR for these branches is reused (only the email is redone);
  // if the earlier PR was completed or abandoned, a new one is created.
  const again = $(".p-again", box);
  again.onclick = guarded(async () => {
    busy(again, true, "Checking");
    let prep;
    try { prep = await getJSON(`/api/code/${entry.id}/pr`); }
    catch (e) { busy(again, false); return toast(e.message, true); }
    renderPrForm(panel, box, entry, prep);
  });
}

// ---------- apply TD review points ----------
const SEV_ORDER = { critical: 0, high: 1, medium: 2, low: 3 };
const STATUS_LABEL = {
  applied: "Applied", not_applicable: "Not applicable", needs_input: "Needs author input",
  failed: "Not applied", open: "Open",
};
const sevClass = (s) => "sev sev-" + ((s || "").toLowerCase().match(/critical|high|medium|low/) || ["none"])[0];
const escapeHtml = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function attachApply(panel, entry) {
  if (!entry || !["td", "code"].includes(entry.kind) || !(entry.points || []).length) return;
  const code = entry.kind === "code";
  let box = $(".apply", panel.node);
  if (!box) { box = document.createElement("div"); box.className = "apply"; $(".r-scroll", panel.node).appendChild(box); }
  const points = entry.points;
  const done = points.some((p) => p.status && p.status !== "open");
  if (done) return renderApplyResult(panel, box, entry, points);

  box.innerHTML = `
    <div class="apply-offer">
      <div class="apply-icon">${code
        ? `<svg viewBox="0 0 24 24"><path d="M8 7l-5 5 5 5M16 7l5 5-5 5"/></svg>`
        : `<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z"/><path d="M14 3v5h5M9 15l2 2 4-4"/></svg>`}</div>
      <div class="apply-text">
        <b>Want ReviewKit to apply these review points to ${code ? "your code" : "the document"}?</b>
        <span class="muted small">${code
          ? `You choose which points to apply. Claude edits the files in your local clone on <b>${escapeHtml(entry.source || "the source branch")}</b>. Nothing is committed or pushed: you review the changes and commit them yourself.`
          : "You choose which points to apply. Edits follow your document's template and appear as tracked changes in a copy. Your original isn't modified."}</span>
      </div>
      <div class="row">
        <button class="btn ghost a-no">No thanks</button>
        <button class="btn primary a-yes">Yes, choose points</button>
      </div>
    </div>`;
  $(".a-no", box).onclick = async () => {
    const what = code ? "code" : "document";
    if (!await ask({ title: `Leave the ${what} as is?`, message: "You can still apply the review points later from History.", ok: "Leave as is" })) return;
    box.innerHTML = `<div class="apply-note muted small">OK, the ${what} was left as is. You can apply points later from History.</div>`; };
  $(".a-yes", box).onclick = () => renderApplySelect(panel, box, entry, points);
}

// back: { label, go } — where the Back/Cancel button returns to (the offer, or the applied results).
function renderApplySelect(panel, box, entry, points, back) {
  back = back || { label: "Cancel", go: () => attachApply(panel, { ...entry, points: points.map((p) => ({ ...p, status: "open" })) }) };
  // Changing an earlier selection starts from what was applied last time.
  const wasTicked = (p) => p.status !== "not_applicable";
  const code = entry.kind === "code";
  const sorted = [...points].sort((a, b) => (SEV_ORDER[(a.severity || "").toLowerCase()] ?? 9) - (SEV_ORDER[(b.severity || "").toLowerCase()] ?? 9));
  box.innerHTML = `
    <div class="apply-head">
      <div><b>Choose the review points to apply</b>
        <div class="muted small">Untick anything that doesn't apply. Unticked points are marked <b>Not applicable</b>, and their parts of the ${code ? "code" : "document"} aren't touched.</div></div>
      <div class="row"><button class="btn tiny ghost a-all">Select all</button><button class="btn tiny ghost a-none">Select none</button></div>
    </div>
    <div class="point-list">${sorted.map((p) => `
      <label class="point">
        <input type="checkbox" value="${escapeHtml(p.id)}"${wasTicked(p) ? " checked" : ""}>
        <div class="point-body">
          <div class="point-top"><span class="${sevClass(p.severity)}">${escapeHtml(p.severity || "—")}</span>
            <span class="point-id">${escapeHtml(p.id)}</span><b>${escapeHtml(p.title)}</b></div>
          ${p.file ? `<div class="muted small point-file">${escapeHtml(p.file)}${p.line ? `:${escapeHtml(p.line)}` : ""}</div>`
            : p.section ? `<div class="muted small">Section: ${escapeHtml(p.section)}</div>` : ""}
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
    if (changed && !await ask({ title: "Discard your changes?", message: "The points you ticked or unticked here are reset. Nothing is applied.", ok: "Discard changes" })) return;
    back.go();
  });
  $(".a-go", box).onclick = guarded(async () => {
    const selected = boxes.filter((b) => b.checked).map((b) => b.value);
    if (code) return confirmCodeApply(panel, box, entry, selected, boxes.length - selected.length, back);
    const already = !!entry.reviewed_name;
    const yes = await ask({
      title: `Apply ${selected.length} review point${selected.length > 1 ? "s" : ""}?`,
      message: (already ? "This replaces the edited copy made earlier. Changes you made to that copy outside ReviewKit are lost unless you saved it elsewhere (Save a copy…). " : "") +
        "Claude writes the edits as tracked changes into a copy of the document. Your original file isn't changed.",
      details: [["Applying", selected.join(", ")], ["Not applicable", `${boxes.length - selected.length} point(s)`]],
      ok: already ? "Replace and apply" : "Apply points",
    });
    if (yes) runApply(panel, box, entry, selected, back);
  });
  sync();
}

async function runApply(panel, box, entry, selected, back) {
  box.innerHTML = `<div class="apply-running"><span class="spinner"></span><span class="a-status">Starting…</span></div>`;
  try {
    const res = await api(`/api/${entry.kind === "code" ? "code" : "td"}/${entry.id}/apply`, { method: "POST", body: { selected } });
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
          toast(entry.kind === "code" ? "Review points applied to your code (not committed)" : "Document updated with tracked changes");
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
  if (entry.kind === "code") return renderCodeResult(panel, box, entry, points);
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

// ---------- apply to code ----------
async function confirmCodeApply(panel, box, entry, selected, skipped, back) {
  let check;
  try { check = await getJSON(`/api/code/${entry.id}/check`); }
  catch (e) { return toast(e.message, true); }
  if (!check.ok) {
    return ask({ title: "Can't apply yet", message: check.error, ok: "OK" });
  }
  const yes = await ask({
    title: `Apply ${selected.length} review point${selected.length > 1 ? "s" : ""} to your code?`,
    message: (check.previous ? "ReviewKit first reverts its previous changes, then applies this selection. " : "") +
      "Claude edits the files in your local clone. Nothing is committed or pushed. You can view the changes and undo them afterwards." +
      (check.warning ? ` Note: ${check.warning}` : ""),
    details: [["Repository", check.repo], ["Branch", check.branch], ["Applying", selected.join(", ")], ["Not applicable", `${skipped} point(s)`]],
    ok: check.previous ? "Replace and apply" : "Apply to code",
  });
  if (yes) runApply(panel, box, entry, selected, back);
}

function renderCodeResult(panel, box, entry, points) {
  const count = (s) => points.filter((p) => p.status === s).length;
  const a = entry.applied;
  const files = a ? [...a.files.map((f) => ["M", f]), ...a.new_files.map((f) => ["A", f])] : [];
  box.innerHTML = `
    <div class="apply-head">
      <div><b>Changes in ${escapeHtml((entry.repo || "").split(/[\\/]/).pop())} · ${escapeHtml(entry.source || "")}</b>
        <div class="muted small">${count("applied")} applied · ${count("not_applicable")} not applicable${count("needs_input") ? ` · ${count("needs_input")} need your input` : ""}${count("failed") ? ` · ${count("failed")} not applied` : ""}.
          ${a ? "Nothing was committed or pushed. Review the changes, then commit them yourself." : "No files were changed."}</div></div>
      <div class="row wrap-row">
        <button class="btn tiny ghost a-change">Change selection</button>
        ${a ? `<button class="btn tiny ghost a-diff">View changes</button>
        <button class="btn tiny ghost danger a-undo">Undo changes</button>` : ""}
        <button class="btn tiny primary a-folder">Open folder</button>
      </div>
    </div>
    ${files.length ? `<div class="changed-files">${files.map(([k, f]) => `<div><span class="fk fk-${k}">${k === "A" ? "new" : "edited"}</span><code></code></div>`).join("")}</div>` : ""}
    <pre class="diff hidden"></pre>
    <div class="point-list">${points.map((p) => `
      <div class="point result-${p.status}">
        <span class="status-pill st-${p.status}">${STATUS_LABEL[p.status] || p.status}</span>
        <div class="point-body">
          <div class="point-top"><span class="${sevClass(p.severity)}">${escapeHtml(p.severity || "—")}</span>
            <span class="point-id">${escapeHtml(p.id)}</span><b>${escapeHtml(p.title)}</b></div>
          ${p.file ? `<div class="muted small point-file">${escapeHtml(p.file)}${p.line ? `:${escapeHtml(p.line)}` : ""}</div>` : ""}
          ${p.note ? `<div class="point-change">${escapeHtml(p.note)}</div>` : ""}
        </div>
      </div>`).join("")}
    </div>`;
  $$(".changed-files code", box).forEach((c, i) => (c.textContent = files[i][1]));
  $(".a-folder", box).onclick = async () => { try { await api(`/api/code/${entry.id}/open-folder`, { method: "POST" }); } catch (e) { toast(e.message, true); } };
  const diffBtn = $(".a-diff", box);
  if (diffBtn) diffBtn.onclick = guarded(async () => {
    const pre = $(".diff", box);
    if (!pre.classList.contains("hidden")) { pre.classList.add("hidden"); diffBtn.textContent = "View changes"; return; }
    try { pre.textContent = (await getJSON(`/api/code/${entry.id}/changes`)).diff || "(no changes)"; }
    catch (e) { return toast(e.message, true); }
    pre.classList.remove("hidden");
    diffBtn.textContent = "Hide changes";
  });
  const undoBtn = $(".a-undo", box);
  if (undoBtn) undoBtn.onclick = guarded(async () => {
    if (!await ask({
      title: "Undo ReviewKit's changes?",
      message: "The edited files go back to how they were before, and files ReviewKit created are deleted. Your own uncommitted work isn't touched.",
      details: [["Files", `${files.length}`]], ok: "Undo changes", danger: true,
    })) return;
    try {
      const r = await getJSON(`/api/code/${entry.id}/undo`, { method: "POST" });
      panel.markdown = r.markdown; panel.entry = r.entry;
      panel.body.innerHTML = renderMarkdown(r.markdown);
      attachApply(panel, r.entry);
      toast("Changes undone");
    } catch (e) { toast(e.message, true); }
  });
  $(".a-change", box).onclick = () => renderApplySelect(panel, box, entry, points, {
    label: "Back to results",
    go: () => renderCodeResult(panel, box, entry, points),
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
async function clearHistory() {
  let items = [];
  try { items = await getJSON("/api/history"); } catch (e) {}
  if (!items.length) return toast("History is already empty.");
  if (!await ask({
    title: "Clear all history?",
    message: "Every saved review on this PC is deleted, including technical design documents stored with them. This can't be undone. Reviews already moved to your archive folder aren't touched.",
    details: [["Reviews", String(items.length)]],
    ok: `Delete ${items.length} review${items.length > 1 ? "s" : ""}`, danger: true,
  })) return;
  try {
    const r = await getJSON("/api/history", { method: "DELETE" });
    state.historyId = null;
    $('[data-slot="history"]').innerHTML = "";
    await loadHistory();
    toast(`Deleted ${r.deleted} review${r.deleted === 1 ? "" : "s"}`);
  } catch (e) { toast(e.message, true); }
}
$("#clearHistoryBtn").onclick = guarded(clearHistory);
$("#clearHistoryBtn2").onclick = guarded(clearHistory);

// ---------- settings ----------
async function loadSettings() {
  await loadStatus();
  const s = await getJSON("/api/settings");
  $("#keyHint").textContent = s.api_key_set ? `A key ending in ${s.api_key_hint} is saved.` : "No API key saved.";
  $$("#modelSel button").forEach((b) => b.classList.toggle("active", b.dataset.model === (s.model || "")));
  renderArchive(s);
  $("#autoUpdate").checked = s.auto_update !== false;
  if (document.activeElement !== $("#workEmail")) $("#workEmail").value = s.work_email || "";
  const st = state.status;
  if (st) $("#curVersion").textContent = st.version;
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
  if (boot && autoUpdate && u.can_install && !reviewRunning() && !bootGate.skipped) return installUpdate();
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
  $("#ovRetry").classList.add("hidden");
  status.textContent = "Checking the latest version…";
  bar.parentElement.classList.remove("indeterminate");
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
    const close = $("#ovClose"), retry = $("#ovRetry");
    close.classList.remove("hidden");
    retry.classList.remove("hidden");
    close.onclick = () => {
      ov.classList.add("hidden");
      // Keep the update offer visible so it can be retried later from the banner or Settings.
      if (updateInfo && updateInfo.available && updateInfo.can_install) $("#updateBar").classList.remove("hidden");
    };
    retry.onclick = guarded(() => installUpdate());
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

// ---------- activity log (Supabase) ----------
function renderActivity(a) {
  const on = !!(a && a.enabled);
  $("#activityCard").classList.toggle("hidden", !on);
  $("#adminNav").classList.toggle("hidden", !(on && a.admin));
  $("#blockedOverlay").classList.toggle("hidden", !(on && a.email && a.active === false));
  if (!on) return;
  $("#blEmail").textContent = a.email || "this account";
  $("#workEmailField").classList.toggle("hidden", !(state.status && state.status.auth && state.status.auth.authMethod === "api_key"));
  const line = $("#activityStatus");
  const [cls, text, sub] = a.needs_email ? ["bad", "Enter your work e-mail below", ""]
    : !a.email ? ["", "Sign in to Claude first", ""]
    : ["ok", `Logged as ${a.email}`, a.offline ? "offline · sent when you're back online" : (a.admin ? "admin" : "")];
  line.innerHTML = `<span class="dot ${cls}"></span><span></span><span class="muted"></span>`;
  $$("span", line)[1].textContent = text;
  $$("span", line)[2].textContent = sub;
}
$("#blRetry").onclick = guarded(async () => { await loadStatus(); });
$("#saveWorkEmail").onclick = guarded(async () => {
  const email = $("#workEmail").value.trim();
  if (!/^[^@\s;,]+@[^@\s;,]+\.[^@\s;,]+$/.test(email)) return toast("Enter a valid work e-mail address.", true);
  try {
    await getJSON("/api/settings", { method: "POST", body: { work_email: email } });
    await loadStatus();
    toast("Work e-mail saved");
  } catch (e) { toast(e.message, true); }
});

// ---------- users & activity (admin page) ----------
const ACTIONS = {
  "app.open": ["Opened ReviewKit", ""],
  "app.update": ["Updated ReviewKit", ""],
  "claude.sign_in": ["Signed in to Claude", ""],
  "claude.sign_out": ["Signed out of Claude", ""],
  "code_review.run": ["Code review", "review"],
  "code_review.apply": ["Applied code points", "change"],
  "code_review.undo": ["Undid applied points", "change"],
  "td_review.run": ["TD review", "review"],
  "td_review.apply": ["Applied TD points", "change"],
  "td_review.save_document": ["Saved reviewed TD", ""],
  "pr.create": ["Pull request", "change"],
  "history.export": ["Exported review", ""],
  "history.delete": ["Deleted review", "danger"],
  "history.clear": ["Cleared history", "danger"],
  "settings.change": ["Changed settings", ""],
};
const admin = { users: [], logs: [], more: false, loaded: false };
const fmtWhen = (iso) => {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  if (d.toDateString() === now.toDateString()) return `Today ${time}`;
  return `${d.toLocaleDateString([], { day: "numeric", month: "short", year: d.getFullYear() === now.getFullYear() ? undefined : "numeric" })} ${time}`;
};
const plural = (n, word) => `${n} ${word}${n === 1 ? "" : "s"}`;
function describeEvent(e) {
  const d = e.details || {};
  switch (e.action) {
    case "code_review.run":
      return [d.repo, d.source && d.target && `${d.source} → ${d.target}`, d.points != null && plural(d.points, "point") + (d.critical ? ` (${d.critical} critical)` : ""),
        d.seconds && `${Math.round(d.seconds)}s`].filter(Boolean).join(" · ");
    case "td_review.run":
      return [d.document, d.points != null && plural(d.points, "point") + (d.critical ? ` (${d.critical} critical)` : ""), d.seconds && `${Math.round(d.seconds)}s`].filter(Boolean).join(" · ");
    case "code_review.apply":
    case "td_review.apply":
      return [d.repo || d.document, d.source, `${d.applied || 0} of ${d.selected || 0} applied`,
        d.needs_input && `${d.needs_input} need input`, d.failed && `${d.failed} failed`, d.files != null && plural(d.files, "file")].filter(Boolean).join(" · ");
    case "pr.create":
      return [`${d.existing ? "Reused" : "Created"} PR ${d.pr_id}`, d.repo, d.source && `${d.source} → ${d.target}`, d.reviewers && plural(d.reviewers, "reviewer")].filter(Boolean).join(" · ");
    case "history.clear": return plural(d.deleted || 0, "review") + " deleted";
    case "settings.change": return (d.keys || []).join(", ");
    case "app.update": return d.from_version ? `from ${d.from_version}` : "";
    default:
      return Object.entries(d).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join(" · ");
  }
}
function adminLocked(locked) {
  $("#adminGate").classList.toggle("hidden", !locked);
  $("#adminPanel").classList.toggle("hidden", locked);
  $("#adminTools").classList.toggle("hidden", locked);
}
async function loadAdmin() {
  let s;
  try { s = await getJSON("/api/admin/session"); } catch (e) { return toast(e.message, true); }
  if (!s.signed_in) {
    adminLocked(true);
    const em = $("#adminEmail");
    if (!em.value) em.value = (state.status && state.status.activity && state.status.activity.email) || "";
    return;
  }
  adminLocked(false);
  if (!admin.loaded) await refreshAdmin();
}
async function adminFetch(path, opts) {
  try { return await getJSON(path, opts); }
  catch (e) {
    if (/Sign in to the Users page|session expired/.test(e.message)) adminLocked(true);
    throw e;
  }
}
async function refreshAdmin() {
  try {
    admin.users = await adminFetch("/api/admin/users");
    admin.loaded = true;
    renderUsers();
    await loadLogs(false);
  } catch (e) { toast(e.message, true); }
}
function renderUsers() {
  $("#adminUserCount").textContent = String(admin.users.length);
  const me = (state.status && state.status.activity && state.status.activity.email) || "";
  const body = $("#adminUsers tbody");
  body.innerHTML = admin.users.length ? "" : `<tr><td colspan="6" class="empty">No one has used ReviewKit yet.</td></tr>`;
  admin.users.forEach((u) => {
    const tr = document.createElement("tr");
    tr.className = u.is_active ? "" : "off";
    tr.innerHTML = `<td class="who"><a></a><span class="muted small"></span></td><td></td><td class="small"></td>
      <td class="nowrap"></td><td class="nowrap muted"></td><td><label class="toggle"><input type="checkbox"><span></span></label></td>`;
    const tds = $$("td", tr);
    $("a", tds[0]).textContent = u.email;
    $("a", tds[0]).title = "Show this user's activity";
    $("a", tds[0]).onclick = () => { $("#adminLogUser").value = u.email; loadLogs(false); };
    $("span", tds[0]).textContent = u.auth_method === "api_key" ? "API key" : "";
    tds[1].textContent = u.claude_org || "—";
    tds[2].textContent = [u.machine, u.app_version].filter(Boolean).join(" · ") || "—";
    tds[3].textContent = fmtWhen(u.last_seen);
    tds[4].textContent = fmtWhen(u.first_seen);
    const box = $("input", tds[5]), lbl = $("span", tds[5]);
    box.checked = u.is_active;
    lbl.textContent = u.is_active ? "Allowed" : "Turned off";
    if (u.email === me) { box.disabled = true; box.parentElement.title = "You can't turn off your own access."; }
    box.onchange = guarded(async () => {
      const on = box.checked;
      if (!on && !await ask({
        title: `Turn off ${u.email}?`,
        message: "They can't run reviews, apply points or create pull requests until you turn them back on. It takes effect within about 10 minutes, or as soon as they restart ReviewKit.",
        ok: "Turn off", danger: true,
      })) { box.checked = true; return; }
      try {
        const row = await adminFetch("/api/admin/users/active", { method: "POST", body: { email: u.email, active: on } });
        Object.assign(u, row);
        renderUsers();
        toast(on ? `${u.email} can use ReviewKit again` : `${u.email} is turned off`);
      } catch (e) { box.checked = !on; toast(e.message, true); }
    });
    body.appendChild(tr);
  });
  const sel = $("#adminLogUser"), keep = sel.value;
  sel.innerHTML = `<option value="">Everyone</option>`;
  admin.users.forEach((u) => { const o = document.createElement("option"); o.value = o.textContent = u.email; sel.appendChild(o); });
  sel.value = admin.users.some((u) => u.email === keep) ? keep : "";
  const act = $("#adminLogAction");
  if (act.options.length === 1) {
    Object.entries(ACTIONS).forEach(([k, [label]]) => { const o = document.createElement("option"); o.value = k; o.textContent = label; act.appendChild(o); });
  }
}
async function loadLogs(older) {
  const q = new URLSearchParams();
  if ($("#adminLogUser").value) q.set("email", $("#adminLogUser").value);
  if ($("#adminLogAction").value) q.set("action", $("#adminLogAction").value);
  if (older && admin.logs.length) q.set("before", admin.logs[admin.logs.length - 1].id);
  try {
    const rows = await adminFetch(`/api/admin/logs?${q}`);
    admin.logs = older ? admin.logs.concat(rows) : rows;
    admin.more = rows.length >= 200;
  } catch (e) { return toast(e.message, true); }
  const body = $("#adminLogs tbody");
  body.innerHTML = admin.logs.length ? "" : `<tr><td colspan="4" class="empty">No activity yet.</td></tr>`;
  admin.logs.forEach((e) => {
    const [label, kind] = ACTIONS[e.action] || [e.action, ""];
    const tr = document.createElement("tr");
    tr.innerHTML = `<td class="nowrap"></td><td></td><td><span class="act"></span></td><td class="details"></td>`;
    const tds = $$("td", tr);
    tds[0].textContent = fmtWhen(e.occurred_at || e.created_at);
    tds[0].title = `On the PC: ${e.occurred_at || "?"}\nReceived: ${e.created_at}${e.machine ? `\nPC: ${e.machine}` : ""}${e.app_version ? `\nVersion: ${e.app_version}` : ""}`;
    tds[1].textContent = e.email;
    $(".act", tds[2]).textContent = label;
    if (kind) $(".act", tds[2]).classList.add(`act-${kind}`);
    tds[3].textContent = describeEvent(e);
    body.appendChild(tr);
  });
  $("#adminMore").classList.toggle("hidden", !admin.more);
}
$("#adminLogUser").addEventListener("change", () => loadLogs(false));
$("#adminLogAction").addEventListener("change", () => loadLogs(false));
$("#adminMore").onclick = guarded(() => loadLogs(true));
$("#adminRefresh").onclick = guarded(async () => { await refreshAdmin(); toast("Refreshed"); });
$("#adminLock").onclick = guarded(async () => {
  await api("/api/admin/sign-out", { method: "POST" });
  admin.loaded = false;
  $("#adminPassword").value = "";
  adminLocked(true);
});
const adminSignIn = guarded(async () => {
  const email = $("#adminEmail").value.trim(), password = $("#adminPassword").value;
  if (!email || !password) return toast("Enter the admin e-mail and password.", true);
  const btn = $("#adminSignIn");
  busy(btn, true, "Signing in");
  try {
    await api("/api/admin/sign-in", { method: "POST", body: { email, password } });
    $("#adminPassword").value = "";
    adminLocked(false);
    await refreshAdmin();
  } catch (e) { toast(e.message, true); }
  finally { busy(btn, false); }
});
$("#adminSignIn").onclick = adminSignIn;
[$("#adminEmail"), $("#adminPassword")].forEach((el) => el.addEventListener("keydown", (e) => { if (e.key === "Enter") adminSignIn(); }));

// ---------- boot ----------
// With automatic updates on, the app stays covered until the update check is done, so an update
// can't take over the window while someone is already working. A slow check can be skipped; an
// update found after that is only offered in the banner.
const bootGate = { skipped: false };
async function bootUpdateCheck() {
  let auto = true;
  try { auto = (await getJSON("/api/settings")).auto_update !== false; } catch (e) {}
  if (!auto) return checkUpdate({});
  const ov = $("#updateOverlay"), bar = $("#ovBar"), close = $("#ovClose");
  $("#ovTitle").textContent = "Checking for updates…";
  $("#ovStatus").textContent = "Making sure you have the latest version of ReviewKit.";
  $("#ovRetry").classList.add("hidden");
  close.classList.add("hidden");
  bar.style.width = "";
  bar.parentElement.classList.add("indeterminate");
  ov.classList.remove("hidden");
  const slow = setTimeout(() => {
    if ($("#ovTitle").textContent !== "Checking for updates…") return;  // already installing an update
    $("#ovStatus").textContent = "The update server is slow to answer.";
    close.textContent = "Continue without checking";
    close.classList.remove("hidden");
    close.onclick = () => { bootGate.skipped = true; ov.classList.add("hidden"); };
  }, 8000);
  try { await checkUpdate({ boot: true }); }
  finally {
    clearTimeout(slow);
    close.textContent = "Continue without updating";
    // Still checking only means no update is being installed: open the app.
    if ($("#ovTitle").textContent === "Checking for updates…") ov.classList.add("hidden");
    bar.parentElement.classList.remove("indeterminate");
  }
}

(async function boot() {
  initTheme();
  bootUpdateCheck();  // covers the app right away; the rest loads underneath
  const ok = await loadStatus();
  try {
    const s = await getJSON("/api/settings");
    renderRecent(s.repos);
    renderArchive(s);
    if (s.last_repo) { $("#repoPath").value = s.last_repo; openRepo(); }
  } catch (e) {}
  if (!ok) { show("settings"); toast("Connect your Claude account to get started."); }
  setInterval(() => checkUpdate({}), 6 * 3600 * 1000);
})();
