
// ExamPartner MVP client (auth + browse + Paystack upgrade) + filters + admin mini tools

const els = (id) => document.getElementById(id);
const apiBaseNoSlash = () => (state.apiBase || "").replace(/\/$/, "");
const FILTERS_PANEL_OPEN = "ep_filters_open";

const PAGE_LIMIT = 20;
let pageOffset = 0;
let lastGoodOffset = 0;
let lastGoodItems = [];

function setViewerOpen(isOpen) {
  document.body.classList.toggle("viewer-open", !!isOpen);
}

function focusViewer() {
  const viewer = els("viewer");
  if (viewer) viewer.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setStatus(msg, kind = "ok") {
  const el = els("status");
  if (!el) return;
  el.className = `status ${kind}`;
  el.textContent = msg || "";
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function trimText(s, n) {
  const t = String(s || "");
  return t.length > n ? t.slice(0, n - 1) + "…" : t;
}

/** ------------------------------
 * State
 * ------------------------------ */
const state = {
  apiBase: localStorage.getItem("apiBase") || "https://exampartner-backend.onrender.com",
  token: localStorage.getItem("token") || "",
  email: localStorage.getItem("email") || "",
  busyPay: false,
  user: null,
  filters: {
    exam: localStorage.getItem("filter_exam") || "",
    year: localStorage.getItem("filter_year") || "",
    subject: localStorage.getItem("filter_subject") || "",
  },
};

/** ------------------------------
 * API helpers
 * ------------------------------ */
async function api(path, opts = {}) {
  const url = apiBaseNoSlash() + path;
  const headers = opts.headers || {};
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  headers["Content-Type"] = headers["Content-Type"] || "application/json";

  try {
    const res = await fetch(url, { ...opts, headers });
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.text();

    if (!res.ok) {
      const msg = data?.detail || data?.message || (typeof data === "string" ? data : "Request failed");
      throw new Error(msg);
    }
    return data;
  } catch (e) {
    setStatus(e?.message || "Network error", "bad");
    throw e;
  }
}

function saveApiBase() {
  const el = els("apiBase");
  if (!el) return;
  const val = (el.value || "").trim();
  if (val) {
    state.apiBase = val;
    localStorage.setItem("apiBase", val);
  }
}

/** ------------------------------
 * Auth
 * ------------------------------ */
async function doRegister() {
  const email = (els("email")?.value || "").trim();
  const password = els("password")?.value || "";
  if (!email || !password) return setStatus("Email and password required.", "bad");

  const r = await api("/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });

  setStatus(r?.message || "Registered.", "ok");
}

async function doLogin() {
  const email = (els("email")?.value || "").trim();
  const password = els("password")?.value || "";
  if (!email || !password) return setStatus("Email and password required.", "bad");

  const r = await api("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });

  if (r?.access_token) {
    state.token = r.access_token;
    state.email = email;
    localStorage.setItem("token", state.token);
    localStorage.setItem("email", email);
  }

  await refreshMe();
  setStatus("Logged in.", "ok");
}

function doLogout() {
  state.token = "";
  state.user = null;
  localStorage.removeItem("token");
  setStatus("Logged out.", "ok");
  updateAuthUI();
  updateUpgradeUI();
}

async function refreshMe() {
  updateAuthUI();
  try {
    if (!state.token) return;
    const me = await api("/auth/me");
    state.user = me;
    updateAuthUI();
    updateUpgradeUI();
  } catch {
    // ignore
  }
}

function updateAuthUI() {
  const pill = els("authState");
  const btnLogout = els("btnLogout");
  if (!pill || !btnLogout) return;

  if (state.token) {
    pill.textContent = state.email || "Logged in";
    btnLogout.hidden = false;
  } else {
    pill.textContent = "Guest";
    btnLogout.hidden = true;
  }
}

/** ------------------------------
 * Filters UI
 * ------------------------------ */
const EXAM_OPTIONS = ["", "NECO", "WAEC", "JAMB"];
const YEAR_OPTIONS = ["", "2025", "2024", "2023", "2022", "2021", "2020"];
const SUBJECT_OPTIONS = ["", "Mathematics", "English", "Biology", "Chemistry", "Physics"];

function fillSelect(el, values) {
  el.innerHTML = "";
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v === "" ? "All" : v;
    el.appendChild(opt);
  }
}

function initFiltersUI() {
  const examSel = els("examFilter");
  const yearSel = els("yearFilter");
  const subjSel = els("subjectFilter");
  if (!examSel || !yearSel || !subjSel) return;

  fillSelect(examSel, EXAM_OPTIONS);
  fillSelect(yearSel, YEAR_OPTIONS);
  fillSelect(subjSel, SUBJECT_OPTIONS);

  examSel.value = state.filters.exam || "";
  yearSel.value = state.filters.year || "";
  subjSel.value = state.filters.subject || "";

  const save = () => {
    state.filters.exam = examSel.value || "";
    state.filters.year = yearSel.value || "";
    state.filters.subject = subjSel.value || "";
    localStorage.setItem("filter_exam", state.filters.exam);
    localStorage.setItem("filter_year", state.filters.year);
    localStorage.setItem("filter_subject", state.filters.subject);
  };

  examSel.onchange = () => { save(); updatePracticeMetaUI(); onFiltersChanged(); };
  yearSel.onchange = () => { save(); updatePracticeMetaUI(); onFiltersChanged(); };
  subjSel.onchange = () => { save(); updatePracticeMetaUI(); onFiltersChanged(); };

  const btnClear = els("btnClearFilters");
  if (btnClear) {
    btnClear.onclick = () => {
      examSel.value = "";
      yearSel.value = "";
      subjSel.value = "";
      save();
      updatePracticeMetaUI();
      onFiltersChanged();
      setStatus("Filters cleared.", "ok");
    };
  }
}

function buildFilterQuery() {
  const params = new URLSearchParams();
  if (state.filters.exam) params.set("exam", state.filters.exam);
  if (state.filters.year) params.set("year", state.filters.year);
  if (state.filters.subject) params.set("subject", state.filters.subject);
  const s = params.toString();
  return s ? `&${s}` : "";
}

function hasRequiredSelection() {
  // For scalable multi-subject/exam UX: require Exam + Subject.
  return !!(state.filters.exam && state.filters.subject);
}

function setPagerState({ canPrev, canNext, info }) {
  const prevBtn = els("btnPagePrev");
  const nextBtn = els("btnPageNext");
  const infoEl = els("pageInfo");
  if (prevBtn) prevBtn.disabled = !canPrev;
  if (nextBtn) nextBtn.disabled = !canNext;
  if (infoEl) infoEl.textContent = info || "";
}

function showStartGate(show) {
  const gate = els("startGate");
  if (gate) gate.hidden = !show;
}

function openFilters() {
  const panel = els("filtersPanel");
  if (panel) panel.open = true;
  if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function resetPagination() {
  pageOffset = 0;
  lastGoodOffset = 0;
  lastGoodItems = [];
  setPagerState({ canPrev: false, canNext: false, info: "" });
}

function onFiltersChanged() {
  // Anytime the user changes exam/year/subject, restart from page 1.
  resetPagination();

  if (!hasRequiredSelection()) {
    els("list").innerHTML = "";
    showStartGate(true);
    setStatus("Select Exam and Subject in Filters to start.", "bad");
    return;
  }

  showStartGate(false);
  loadList({ offset: 0, reason: "filters_changed" });
}

/** ------------------------------
 * Question list + viewer
 * ------------------------------ */
let currentListIds = [];
let currentIndex = -1;

function renderList(items) {
  const list = els("list");
  list.innerHTML = "";

  if (!items || !items.length) {
    list.innerHTML = `<div class="status">No questions found. Try clearing filters.</div>`;
    currentListIds = [];
    currentIndex = -1;
    return;
  }

  currentListIds = items.map((x) => x.id);
  currentIndex = -1;

  for (const q of items) {
    const div = document.createElement("div");
    div.className = "item";
    div.onclick = () => openQuestion(q.id);

    const meta = [];
    if (q.type) meta.push(q.type);
    if (q.paper) meta.push(q.paper);
    if (q.section) meta.push(q.section);
    if (q.marks) meta.push(`${q.marks} marks`);
    if (q.page) meta.push(`page ${q.page}`);

    div.innerHTML = `
      <div class="id">${escapeHtml(q.id || "")}</div>
      <div class="txt">${escapeHtml(trimText(q.question_text, 140))}</div>
      <div class="meta">${escapeHtml(meta.join(" • "))}</div>
    `;
    list.appendChild(div);
  }
}

async function loadList(options = {}) {
  saveApiBase();
  const mode = els("mode").value;
  const limit = PAGE_LIMIT;

  // Guard: require selection before loading
  if (!hasRequiredSelection()) {
    els("list").innerHTML = "";
    showStartGate(true);
    setStatus("Select Exam and Subject in Filters to start.", "bad");
    setPagerState({ canPrev: false, canNext: false, info: "" });
    return;
  }

  showStartGate(false);

  const offset = typeof options.offset === "number" ? options.offset : pageOffset;

  els("paywall").hidden = true;
  setStatus("Loading…", "ok");

  const filterQs = buildFilterQuery();
  const r = await api(`/questions/${mode}?limit=${limit}&offset=${offset}${filterQs}`);

  if (r?.paywall) {
    // Unpaid users reached preview cap
    setStatus("Preview limit reached. Please upgrade to continue.", "bad");
    els("list").innerHTML = "";
    els("paywall").hidden = false;
    // disable next/prev while paywalled
    setPagerState({ canPrev: offset > 0, canNext: false, info: "Upgrade required" });
    return;
  }

  const items = r?.items || [];

  // End reached for paid users (or for the current filter set)
  if (!items.length) {
    if (offset === 0) {
      renderList(items);
      setStatus("No questions found for these filters.", "bad");
      setPagerState({ canPrev: false, canNext: false, info: "" });
      return;
    }

    // Revert to last good page and disable Next
    pageOffset = lastGoodOffset;
    renderList(lastGoodItems);
    setStatus("End reached. No more questions.", "bad");
    const canPrev = pageOffset > 0;
    setPagerState({
      canPrev,
      canNext: false,
      info: pageOffset ? `Showing page starting at ${pageOffset}` : "Showing first page",
    });
    return;
  }

  // Success: store page
  pageOffset = offset;
  lastGoodOffset = offset;
  lastGoodItems = items;

  renderList(items);

  const startNum = pageOffset + 1;
  const endNum = pageOffset + items.length;
  const canPrev = pageOffset > 0;
  const canNext = items.length === limit; // optimistic
  setPagerState({ canPrev, canNext, info: `Showing ${startNum}–${endNum}` });

  setStatus(`Loaded ${items.length} questions.`, "ok");
}

async function openQuestion(qid) {
  const q = await api(`/question/${encodeURIComponent(qid)}`);
  const viewer = els("viewer");
  if (!viewer) return;

  viewer.hidden = false;
  setViewerOpen(true);

  const title = els("qTitle");
  if (title) title.textContent = q?.id || "Question";

  const meta = els("qMeta");
  if (meta) {
    const parts = [];
    if (q.exam) parts.push(q.exam);
    if (q.year) parts.push(q.year);
    if (q.subject) parts.push(q.subject);
    if (q.paper) parts.push(q.paper);
    if (q.section) parts.push(q.section);
    meta.textContent = parts.filter(Boolean).join(" • ");
  }

  const text = els("qText");
  if (text) text.textContent = q?.question_text || "";

  const di = els("qDiagrams");
  if (di) {
    di.innerHTML = "";
    const ds = q?.diagrams || [];
    for (const src of ds) {
      const img = document.createElement("img");
      img.src = apiBaseNoSlash() + (src.startsWith("/") ? src : `/${src}`);
      img.alt = "diagram";
      di.appendChild(img);
    }
  }

  const opts = els("qOptions");
  if (opts) {
    opts.innerHTML = "";
    const arr = q?.options || [];
    if (arr?.length) {
      for (const o of arr) {
        const div = document.createElement("div");
        div.className = "opt";
        div.textContent = o;
        opts.appendChild(div);
      }
    }
  }

  const ans = els("qAnswer");
  if (ans) ans.textContent = q?.answer || "";

  const ex = els("qExplain");
  if (ex) ex.textContent = q?.explanation || "";

  currentIndex = currentListIds.indexOf(qid);
  focusViewer();
}

function closeViewer() {
  const viewer = els("viewer");
  if (viewer) viewer.hidden = true;
  setViewerOpen(false);
}

/** ------------------------------
 * Payments
 * ------------------------------ */
function updateUpgradeUI() {
  const btnPay = els("btnPay");
  const btnCheckPaid = els("btnCheckPaid");
  if (!btnPay || !btnCheckPaid) return;

  if (state.busyPay) {
    btnPay.disabled = true;
    btnCheckPaid.disabled = true;
    return;
  }

  btnPay.disabled = false;
  btnCheckPaid.disabled = false;

  const hint = els("payHint");
  if (hint) {
    hint.textContent = state.user?.paid ? "You’re already upgraded ✅" : "";
  }
}

async function startUpgrade() {
  state.busyPay = true;
  updateUpgradeUI();

  try {
    const r = await api("/payments/init", { method: "POST", body: JSON.stringify({}) });
    if (r?.authorization_url) {
      window.location.href = r.authorization_url;
    } else {
      setStatus("Unable to start payment.", "bad");
    }
  } finally {
    state.busyPay = false;
    updateUpgradeUI();
  }
}

async function checkPaidStatus() {
  await refreshMe();
  if (state.user?.paid) {
    els("paywall").hidden = true;
    setStatus("Upgrade confirmed ✅", "ok");
    // after confirming, load again (if selection exists)
    if (hasRequiredSelection()) loadList({ offset: pageOffset, reason: "paid_confirmed" });
  } else {
    setStatus("Not upgraded yet. If you paid, wait 1–2 minutes and try again.", "bad");
  }
}

/** ------------------------------
 * Practice meta (optional UI tweaks)
 * ------------------------------ */
function updatePracticeMetaUI() {
  // You can expand this later (e.g. show selected exam/year/subject in the hero)
}

/** ------------------------------
 * API health check
 * ------------------------------ */
async function checkApi() {
  saveApiBase();
  const r = await api("/health");
  setStatus(r?.status ? `API OK: ${r.status}` : "API OK", "ok");
}

/** ------------------------------
 * Init
 * ------------------------------ */
function init() {
  els("yr").textContent = new Date().getFullYear();
  els("apiBase").value = state.apiBase;

  // Hide advanced server tools in production
  const params = new URLSearchParams(window.location.search);
  const isLocal = ["localhost", "127.0.0.1"].includes(window.location.hostname);
  const devMode = isLocal || params.has("dev"); // use https://your-site.netlify.app/?dev=1

  if (!devMode) {
    // Force prod API base and prevent users from changing it
    state.apiBase = "https://exampartner-backend.onrender.com";
    localStorage.removeItem("apiBase");

    const apiBaseEl = els("apiBase");
    if (apiBaseEl && apiBaseEl.parentElement) apiBaseEl.parentElement.hidden = true;

    const btnCheck = els("btnCheck");
    if (btnCheck) btnCheck.hidden = true;
  }

  initFiltersUI();

  // B + C: Start Screen (first run) then remember last selection
  if (!hasRequiredSelection()) {
    showStartGate(true);
    setStatus("Select Exam and Subject in Filters to start.", "bad");
    setPagerState({ canPrev: false, canNext: false, info: "" });
    const panel = els("filtersPanel");
    if (panel) panel.open = true;
  } else {
    showStartGate(false);
    resetPagination();
    loadList({ offset: 0, reason: "autoload" });
  }

  // A2: remember filters panel open/closed
  const fp = els("filtersPanel");
  if (fp) {
    const saved = localStorage.getItem(FILTERS_PANEL_OPEN);
    if (saved === "1") fp.open = true;
    fp.addEventListener("toggle", () => {
      localStorage.setItem(FILTERS_PANEL_OPEN, fp.open ? "1" : "0");
    });
  }

  els("btnCheck").onclick = checkApi;
  els("btnRegister").onclick = doRegister;
  els("btnLogin").onclick = doLogin;
  els("btnLogout").onclick = doLogout;

  const btnPrevPage = els("btnPagePrev");
  const btnNextPage = els("btnPageNext");
  if (btnPrevPage) btnPrevPage.onclick = () => {
    if (pageOffset <= 0) return;
    loadList({ offset: Math.max(0, pageOffset - PAGE_LIMIT), reason: "prev_page" });
  };
  if (btnNextPage) btnNextPage.onclick = () => {
    loadList({ offset: pageOffset + PAGE_LIMIT, reason: "next_page" });
  };

  const btnOpenFilters = els("btnOpenFilters");
  if (btnOpenFilters) btnOpenFilters.onclick = openFilters;

  els("btnClose").onclick = closeViewer;

  const btnPractice = els("btnPractice");
  if (btnPractice) btnPractice.onclick = () => {
    resetPagination();
    if (!hasRequiredSelection()) {
      showStartGate(true);
      openFilters();
      return;
    }
    loadList({ offset: 0, reason: "practice_click" });
    const list = els("list");
    if (list) list.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  els("btnPay").onclick = startUpgrade;
  els("btnCheckPaid").onclick = checkPaidStatus;

  const btnPrev = els("btnPrev");
  if (btnPrev) {
    btnPrev.onclick = () => {
      if (!currentListIds.length) return;
      if (currentIndex > 0) openQuestion(currentListIds[currentIndex - 1]);
    };
  }

  const btnNext = els("btnNext");
  if (btnNext) {
    btnNext.onclick = () => {
      if (!currentListIds.length) return;
      if (currentIndex >= 0 && currentIndex < currentListIds.length - 1) {
        openQuestion(currentListIds[currentIndex + 1]);
      }
    };
  }

  refreshMe();
  updateUpgradeUI();
}

init();
