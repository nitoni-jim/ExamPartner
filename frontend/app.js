

// ExamPartner MVP client (auth + browse + Paystack upgrade) + filters + admin mini tools

const els = (id) => document.getElementById(id);
const apiBaseNoSlash = () => (state.apiBase || "").replace(/\/$/, "");
const FILTERS_PANEL_OPEN = "ep_filters_open";
const FILTER_CACHE_KEY = "ep_filter_cache_v1";
const FILTER_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours
const DEBUG_QUESTIONS = true;

const SUPPORT_CONTACT = Object.freeze({
  email: "exampartnerteam@gmail.com",
  phoneDisplay: "+234 803 528 0334",
  phoneHref: "+2348000000000",
});

const FEEDBACK_CATEGORIES = Object.freeze([
  "Bug Report",
  "Feature Request",
  "Content Issue",
  "User Experience",
  "General Feedback",
]);

const QUESTION_FEEDBACK_CATEGORIES = Object.freeze([
  "wrong answer",
  "typo",
  "unclear wording",
  "missing diagram",
  "explanation issue",
  "other",
]);

function diagramSrc(name) {
  const raw = String(name || "").trim();
  if (!raw) return "";

  const parts = raw.split("_");
  if (parts.length >= 2) {
    const exam = parts[0];
    const year = parts[1];
    return `${apiBaseNoSlash()}/static/diagrams/${encodeURIComponent(exam)}/${encodeURIComponent(year)}/${encodeURIComponent(raw)}`;
  }

  // fallback for old files
  return `${apiBaseNoSlash()}/static/diagrams/${encodeURIComponent(raw)}`;
}

const DIAGRAM_LIGHTBOX_MIN_ZOOM = 1;
const DIAGRAM_LIGHTBOX_MAX_ZOOM = 4;
const DIAGRAM_LIGHTBOX_ZOOM_STEP = 0.25;
const DIAGRAM_LIGHTBOX_CLOSE_ANIMATION_MS = 180;

const diagramLightboxState = {
  isOpen: false,
  scale: 1,
  opener: null,
};

function createDiagramImage(name, extraClass = "") {
  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = name;
  img.className = `diagram-img ${extraClass}`.trim();
  img.src = diagramSrc(name);
  img.dataset.diagramName = name;
  img.dataset.zoomableDiagram = "true";
  return img;
}

function getDiagramLightboxElements() {
  return {
    overlay: els("diagramLightbox"),
    image: els("diagramLightboxImage"),
    label: els("diagramLightboxLabel"),
    zoomOut: els("btnDiagramZoomOut"),
    zoomReset: els("btnDiagramZoomReset"),
  };
}

function clampDiagramZoom(scale) {
  return Math.max(DIAGRAM_LIGHTBOX_MIN_ZOOM, Math.min(DIAGRAM_LIGHTBOX_MAX_ZOOM, scale));
}

function syncDiagramLightboxZoom() {
  const { image, zoomOut, zoomReset } = getDiagramLightboxElements();
  if (!image) return;

  image.style.transform = `scale(${diagramLightboxState.scale})`;
  if (zoomOut) zoomOut.disabled = diagramLightboxState.scale <= DIAGRAM_LIGHTBOX_MIN_ZOOM;
  if (zoomReset) zoomReset.disabled = diagramLightboxState.scale === 1;
}

function setDiagramLightboxZoom(scale) {
  diagramLightboxState.scale = clampDiagramZoom(scale);
  syncDiagramLightboxZoom();
}

function resetDiagramLightboxZoom() {
  setDiagramLightboxZoom(1);
}

function openDiagramLightboxFromImage(img) {
  const { overlay, image, label } = getDiagramLightboxElements();
  if (!overlay || !image || !img?.src) return;

  diagramLightboxState.isOpen = true;
  diagramLightboxState.opener = img;
  overlay.hidden = false;
  overlay.classList.remove("is-closing");
  overlay.setAttribute("aria-hidden", "false");

  image.src = img.currentSrc || img.src;
  image.alt = img.alt || "Expanded diagram preview";

  const diagramName = String(img.dataset.diagramName || img.alt || "Diagram").trim() || "Diagram";
  if (label) label.textContent = diagramName;

  resetDiagramLightboxZoom();

  requestAnimationFrame(() => {
    els("btnDiagramClose")?.focus();
  });
}

function closeDiagramLightbox({ restoreFocus = true } = {}) {
  const { overlay, image } = getDiagramLightboxElements();
  if (!overlay || overlay.hidden) return;

  diagramLightboxState.isOpen = false;
  overlay.classList.add("is-closing");
  overlay.setAttribute("aria-hidden", "true");

  window.setTimeout(() => {
    overlay.hidden = true;
    overlay.classList.remove("is-closing");
    if (image) {
      image.removeAttribute("src");
      image.alt = "Expanded diagram preview";
    }
  }, DIAGRAM_LIGHTBOX_CLOSE_ANIMATION_MS);

  resetDiagramLightboxZoom();

  if (restoreFocus && diagramLightboxState.opener && typeof diagramLightboxState.opener.focus === "function") {
    diagramLightboxState.opener.focus();
  }
  diagramLightboxState.opener = null;
}

function handleDiagramLightboxClick(event) {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return;

  const diagramImg = target.closest("img.diagram-img");
  if (!diagramImg || target.closest("#diagramLightbox")) return;

  openDiagramLightboxFromImage(diagramImg);
}

function setViewerOpen(isOpen) {
  document.body.classList.toggle("viewer-open", !!isOpen);
}

function focusViewer() {
  const viewer = els("viewer");
  if (!viewer) return;

  // Re-trigger the flash animation
  viewer.classList.remove("viewer-flash");
  void viewer.offsetWidth; // force reflow
  viewer.classList.add("viewer-flash");

  // ✅ Auto-scroll so user cannot miss it
  viewer.scrollIntoView({ behavior: "smooth", block: "start" });
}

let activeQuestionId = null;

// Viewer navigation + option state
let currentListIds = [];      // IDs from the current rendered list
let currentIndex = -1;        // index of activeQuestionId within currentListIds
let selectedOptionKey = null; // visual-only option highlight
let practiceOpenRequestSeq = 0;

function highlightQuestionCard(qid) {
  const items = document.querySelectorAll(".item");
  items.forEach((el) => {
    if (el.dataset.qid === qid) {
      el.classList.add("active-question");
    } else {
      el.classList.remove("active-question");
    }
  });
}

function clearQuestionHighlight() {
  const items = document.querySelectorAll(".item");
  items.forEach((el) => el.classList.remove("active-question"));
  activeQuestionId = null;
}

function ensureActiveCardVisibleInList(qid) {
  const list = els("list");
  if (!list) return;

  const el = list.querySelector(`.item[data-qid="${CSS.escape(qid)}"]`);
  if (!el) return;

  // Best UX: make selected question actually visible (centered) inside the list container
  el.scrollIntoView({ behavior: "smooth", block: "center", inline: "nearest" });
}

function syncCurrentIndexFromId(qid) {
  currentIndex = currentListIds.indexOf(qid);
}

function updatePrevNextButtons() {
  const bPrev = els("btnPrev");
  const bNext = els("btnNext");
  if (!bPrev || !bNext) return;

  bPrev.disabled = currentIndex <= 0;
  bNext.disabled = currentIndex < 0 || currentIndex >= currentListIds.length - 1;
}

function clearOptionSelection() {
  selectedOptionKey = null;
  const optBox = els("qOptions");
  if (!optBox) return;
  optBox.querySelectorAll(".opt").forEach((el) => el.classList.remove("selected"));
}


 function renderDiagramsInto(containerEl, diagrams, opts = {}) {
  if (!containerEl) return;

  const {
    variant = "block",   // "block" | "inline"
    append = false,      // if true, do NOT clear container first
    extraClass = "",     // extra styling hook if you want more
    title = ""           // optional heading like "Diagrams" / "Answer Diagram"
  } = opts;

  if (!append) containerEl.innerHTML = "";
  if (!Array.isArray(diagrams) || diagrams.length === 0) return;

  // Optional title for grouping (useful for answer/explanation)
  if (title) {
    const h = document.createElement("div");
    h.className = "diag-title";
    h.textContent = title;
    containerEl.appendChild(h);
  }

  const classForVariant = variant === "inline" ? "inline-diagram" : "";
  const finalExtra = [classForVariant, extraClass].filter(Boolean).join(" ");

  for (const name of diagrams) {
    containerEl.appendChild(createDiagramImage(name, finalExtra));
  }
}

// Backwards-compatible: renders MAIN question diagrams into #qDiagrams
function renderDiagrams(diagrams) {
  renderDiagramsInto(els("qDiagrams"), diagrams, { variant: "block" });
}

// HTML form (for inline blocks like sub-questions / explanations)
function renderDiagramsHtml(diagrams) {
  if (!Array.isArray(diagrams) || diagrams.length === 0) return "";

  const imgs = diagrams
    .map((name) => {
      const src = diagramSrc(name);
      const diagramName = escapeHtml(name);
      return `<img 
                class="diagram-img inline-diagram" 
                loading="lazy" 
                alt="${diagramName}" 
                data-diagram-name="${diagramName}"
                data-zoomable-diagram="true"
                src="${src}">
              `;
    })
    .join("");

  return `<div class="subq-diagrams">${imgs}</div>`;
}

// Escape + preserve line breaks + allow explicit diagram placeholders:
// Use: [[diagram:FILE.png]] anywhere in question_text / explanation / steps text
// ====== Rendering helpers ======
function renderTextWithDiagrams(rawText, ctx = {}) {
  const safe = escapeHtml(String(rawText || ""));
  const withBreaks = safe.replace(/\n/g, "<br>");

  const question = ctx.question || null;
  const tables = ctx.tables || question?.tables || {};
  const mode = ctx.mode || "question"; // "question" | "reveal" | "explain"

  // 1) Inject TABLE placeholders: [[table:T1]] or [[table:T1:answer]]
  let out = withBreaks.replace(/\[\[table:([^\]:\]]+)(?::(answer))?\]\]/gi, (_m, key, answerFlag) => {
    const k = String(key || "").trim();
    if (!k) return "";

    const tableObj = tables?.[k];
    if (!tableObj) return `<div class="status">[Missing table: ${escapeHtml(k)}]</div>`;

    const tableMode = answerFlag ? "reveal" : mode;
    return renderTableHtml(tableObj, { title: "", mode: tableMode });
  });

  // 2) Inject DIAGRAM placeholders: [[diagram:FILE.png]]
  out = out.replace(/\[\[diagram:([^\]]+)\]\]/gi, (_m, name) => {
    const file = String(name || "").trim();
    if (!file) return "";

    const alt = escapeHtml(file);
    const src = diagramSrc(file);
    return `<div class="diagrams"><img loading="lazy" alt="${alt}" class="diagram-img inline-diagram" data-diagram-name="${alt}" data-zoomable-diagram="true" src="${src}"></div>`;
  });

  return out;
}

function normalizeColumns(cols) {
  if (!Array.isArray(cols)) return [];
  return cols.map((x) => String(x ?? "").trim()).filter(Boolean);
}

function cellToText(v) {
  if (v === null || v === undefined) return "";
  // numbers, booleans, strings are okay
  if (typeof v === "string") return v;
  if (typeof v === "number" || typeof v === "boolean") return String(v);
  // objects/arrays -> JSON
  try { return JSON.stringify(v); } catch { return String(v); }
}

// Renders ONE table object to HTML string (safe: we escape cells)
function renderTableHtml(tableObj, { title = "", mode = "question" } = {}) {
  if (!tableObj || typeof tableObj !== "object") return "";

  const columns = normalizeColumns(tableObj.columns || tableObj.headers);
  const hasColumns = columns.length > 0;

  // choose which rows to show
  // - question mode: show rows
  // - reveal/explain: if answer_rows exists, show that; else show rows
  const baseRows = Array.isArray(tableObj.rows) ? tableObj.rows : [];
  const ansRows = Array.isArray(tableObj.answer_rows) ? tableObj.answer_rows : [];
  const rowsToUse = (mode !== "question" && ansRows.length) ? ansRows : baseRows;

  if (!hasColumns && rowsToUse.length === 0) return "";

  // if no columns provided, infer columns from first row object keys
  let finalCols = columns;
  if (!finalCols.length && rowsToUse.length) {
    const r0 = rowsToUse[0];
    if (r0 && typeof r0 === "object" && !Array.isArray(r0)) {
      finalCols = Object.keys(r0);
    }
  }

  const ths = finalCols.map((c) => `<th>${escapeHtml(c)}</th>`).join("");

  const trs = rowsToUse.map((r) => {
    // row can be array OR object
    if (Array.isArray(r)) {
      const tds = finalCols.length
        ? finalCols.map((_c, idx) => `<td>${escapeHtml(cellToText(r[idx]))}</td>`).join("")
        : r.map((cell) => `<td>${escapeHtml(cellToText(cell))}</td>`).join("");
      return `<tr>${tds}</tr>`;
    }
    if (r && typeof r === "object") {
      const tds = finalCols.map((c) => `<td>${escapeHtml(cellToText(r[c]))}</td>`).join("");
      return `<tr>${tds}</tr>`;
    }
    // fallback
    return `<tr><td>${escapeHtml(cellToText(r))}</td></tr>`;
  }).join("");

  const titleHtml = title ? `<div class="qtable-title">${escapeHtml(title)}</div>` : "";
  return `
    <div class="qtable-wrap">
      ${titleHtml}
      <table class="qtable">
        <thead><tr>${ths}</tr></thead>
        <tbody>${trs}</tbody>
      </table>
    </div>
  `;
}

// Renders many tables into a container (DOM)
function renderTablesInto(containerEl, tablesObj, refs = null, mode = "question") {
  if (!containerEl) return;
  containerEl.innerHTML = "";

  if (!tablesObj || typeof tablesObj !== "object") return;

  const keys = Array.isArray(refs) && refs.length
    ? refs.map(String)
    : Object.keys(tablesObj);

  if (!keys.length) return;

  for (const k of keys) {
    const t = tablesObj[k];
    const html = renderTableHtml(t, { title: k, mode });
    if (html) {
      const holder = document.createElement("div");
      holder.innerHTML = html;
      containerEl.appendChild(holder);
    }
  }
}

function scrollToExplainBox() {
  const exp = els("qExplain");
  if (!exp) return;

  // Ensure it’s visible before scrolling
  exp.hidden = false;

  exp.scrollIntoView({ behavior: "smooth", block: "start" });
}


// ====== CONFIG ======
const PAYSTACK_AMOUNT_NGN = 1000; // ₦1,000
const PAYSTACK_CURRENCY = "NGN";
const PAYSTACK_CORE_AMOUNT_NGN = 10000; // ₦10,000 (Core annual)
// ====================

// ---- Filter presets ----
// These are FALLBACKS only. Real values are loaded from the backend (/filters).
const EXAM_OPTIONS = ["", "NECO", "WAEC", "JAMB"];
const SUBJECT_OPTIONS = ["", "Mathematics"];
const YEAR_OPTIONS = (() => {
  const now = new Date().getFullYear();
  const years = [""];
  for (let y = now; y >= 2000; y--) years.push(String(y));
  return years;
})();

let FILTER_CACHE = { exams: EXAM_OPTIONS.slice(1), years: YEAR_OPTIONS.slice(1).map(Number).filter(Boolean), subjects: SUBJECT_OPTIONS.slice(1) };

// Admin key stored ONLY in sessionStorage
const ADMIN_KEY_STORAGE = "ep_admin_key";

const params = new URLSearchParams(window.location.search);
const isDev = params.has("dev");

const state = {
  apiBase:
    localStorage.getItem("apiBase") ||
    (isDev ? "https://proattack-unfurcate-cherise.ngrok-free.dev"
           : "https://exampartner-backend.onrender.com"),
 
  token: sessionStorage.getItem("token") || "",
  
  isPaid: false,
  isAdmin: false,
  authenticated: false,
  freeLimit: 10,
  busyPay: false,

  
 historyOpen: false,
 historyLoadedOnce : false,

  // list paging (offset is internal)
  pageSize: 20,
  pageIndex: 0,
  endReached: false,
  paywalled: false,
  lastItems: [],

  hasLoadedQuestions: false, // ✅ NEW: user has attempted to load questions


  filters: {
    exam: localStorage.getItem("filter_exam") || "",
    year: localStorage.getItem("filter_year") || "",
    subject: localStorage.getItem("filter_subject") || "",
  },

  adminKey: sessionStorage.getItem(ADMIN_KEY_STORAGE) || "",
  adminView: "dashboard",
  adminQuestions: [],
  adminFeedback: [],
  devMode: false,
  cbt: {
    loading: false,
    questions: [],
    currentIndex: 0,
    selectedOptionKey: null,
    answersByQuestionKey: {},
    sessionReady: false,
    timerDurationMs: 0,
    timeRemainingMs: 0,
    timerStartedAt: 0,
    timerIntervalId: null,
    timerExpired: false,
    submitted: false,
    result: null,
    feedbackOpen: false,
  },
};

function setStatus(msg, kind = "ok") {
  const el = els("status");
  el.textContent = msg;
  el.className = `status ${kind}`;
}

function setAuthMsg(msg) {
  els("authMsg").textContent = msg || "";
}

function setPayMsg(msg) {
  els("payMsg").textContent = msg || "";
}

function setDashboardMsg(msg) {
  const el = els("dashboardMsg");
  if (el) el.textContent = msg || "";
}

function setStatusText(el, msg, kind = "") {
  if (!el) return;

  el.textContent = msg || "";
  el.style.color = kind === "bad"
    ? "rgba(251, 113, 133, 0.95)"
    : kind === "ok"
      ? "rgba(52, 211, 153, 0.95)"
      : "";
}

function setFeedbackStatus(msg, kind = "") {
  setStatusText(els("feedbackStatus"), msg, kind);
}

function setQuestionFeedbackStatus(msg, kind = "") {
  setStatusText(els("questionFeedbackStatus"), msg, kind);
}

function setCbtStatus(msg, kind = "") {
  setStatusText(els("cbtStatus"), msg, kind);
}

function getSupportModalElements(kind) {
  if (kind === "contact") {
    return {
      modal: els("contactUsModal"),
      close: els("btnCloseContactModal"),
      opener: els("btnFooterContact"),
    };
  }

  if (kind === "feedback") {
    return {
      modal: els("feedbackModal"),
      close: els("btnCloseFeedbackModal"),
      opener: els("btnFooterFeedback"),
    };
  }

  return { modal: null, close: null, opener: null };
}

function closeSupportModal(kind, { restoreFocus = true } = {}) {
  const { modal, opener } = getSupportModalElements(kind);
  if (!modal) return;

  modal.hidden = true;
  modal.setAttribute("aria-hidden", "true");

  if (restoreFocus && opener) opener.focus();
}

function openSupportModal(kind) {
  const { modal, close } = getSupportModalElements(kind);
  if (!modal) return;

  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");

  requestAnimationFrame(() => {
    if (close) close.focus();
  });
}

function populateContactUi() {
  const emailEl = els("contactSupportEmail");
  const phoneEl = els("contactSupportPhone");

  if (emailEl) {
    emailEl.textContent = SUPPORT_CONTACT.email;
    emailEl.href = `mailto:${SUPPORT_CONTACT.email}`;
  }

  if (phoneEl) {
    phoneEl.textContent = SUPPORT_CONTACT.phoneDisplay;
    phoneEl.href = `tel:${SUPPORT_CONTACT.phoneHref}`;
  }
}

function populateSelectOptions(el, values, placeholder = "Select an option") {
  if (!el) return;

  el.innerHTML = `<option value="">${escapeHtml(placeholder)}</option>`;

  for (const value of values) {
    const opt = document.createElement("option");
    opt.value = value;
    opt.textContent = value;
    el.appendChild(opt);
  }
}

function populateFeedbackCategories() {
  populateSelectOptions(els("feedbackCategory"), FEEDBACK_CATEGORIES, "Select a category");
}

function populateQuestionFeedbackCategories() {
  populateSelectOptions(els("questionFeedbackCategory"), QUESTION_FEEDBACK_CATEGORIES, "Select a category");
  populateSelectOptions(els("cbtQuestionFeedbackCategory"), QUESTION_FEEDBACK_CATEGORIES, "Select a category");
}

function getFeedbackDraft() {
  return {
    category: String(els("feedbackCategory")?.value || "").trim(),
    message: String(els("feedbackMessage")?.value || "").trim(),
    source_area: "footer",
  };
}

function getQuestionFeedbackDraft() {
  return {
    question_id: String(state.currentQuestion?.id || activeQuestionId || "").trim(),
    category: String(els("questionFeedbackCategory")?.value || "").trim(),
    message: String(els("questionFeedbackMessage")?.value || "").trim(),
    source_area: "practice",
  };
}

function getCbtQuestionFeedbackDraft() {
  const question = state.cbt.questions[state.cbt.currentIndex] || null;
  return {
    question_id: String(question?.id || question?.qid || question?._id || "").trim(),
    category: String(els("cbtQuestionFeedbackCategory")?.value || "").trim(),
    message: String(els("cbtQuestionFeedbackMessage")?.value || "").trim(),
    source_area: "cbt",
  };
}

function setQuestionFeedbackPanelOpen(isOpen, { resetStatus = false, focusMessage = false } = {}) {
  const panel = els("questionFeedbackPanel");
  const btn = els("btnReportQuestion");
  if (!panel || !btn) return;

  panel.hidden = !isOpen;
  btn.setAttribute("aria-expanded", isOpen ? "true" : "false");

  if (resetStatus) setQuestionFeedbackStatus("");

  if (isOpen && focusMessage) {
    requestAnimationFrame(() => {
      els("questionFeedbackCategory")?.focus();
    });
  }
}

function resetQuestionFeedbackForm({ keepStatus = false } = {}) {
  const form = els("questionFeedbackForm");
  if (form) form.reset();
  if (!keepStatus) setQuestionFeedbackStatus("");
}

function setCbtQuestionFeedbackStatus(msg, kind = "") {
  setStatusText(els("cbtQuestionFeedbackStatus"), msg, kind);
}

function setCbtQuestionFeedbackPanelOpen(isOpen, { resetStatus = false, focusMessage = false } = {}) {
  const panel = els("cbtQuestionFeedbackPanel");
  const btn = els("btnCbtReportQuestion");
  if (!panel || !btn) return;

  panel.hidden = !isOpen;
  btn.setAttribute("aria-expanded", isOpen ? "true" : "false");
  state.cbt.feedbackOpen = !!isOpen;

  if (resetStatus) setCbtQuestionFeedbackStatus("");

  if (isOpen && focusMessage) {
    requestAnimationFrame(() => {
      els("cbtQuestionFeedbackCategory")?.focus();
    });
  }
}

function resetCbtQuestionFeedbackForm({ keepStatus = false } = {}) {
  const form = els("cbtQuestionFeedbackForm");
  if (form) form.reset();
  if (!keepStatus) setCbtQuestionFeedbackStatus("");
}

async function submitQuestionFeedback(payload) {
  const normalizedPayload = {
    question_id: String(payload?.question_id || "").trim(),
    category: String(payload?.category || "").trim(),
    message: String(payload?.message || "").trim(),
    source_area: String(payload?.source_area || "practice").trim() || "practice",
  };

  const response = await api("/feedback/question", {
    method: "POST",
    body: JSON.stringify(normalizedPayload),
  });

  if (response?.ok === false) return response;

  return { ok: true, id: response?.id || "" };
}

async function handleQuestionFeedbackSubmit(event) {
  if (event) event.preventDefault();

  const payload = getQuestionFeedbackDraft();

  if (!payload.question_id) {
    setQuestionFeedbackStatus("Open a question before submitting a report.", "bad");
    return;
  }

  if (!payload.category) {
    setQuestionFeedbackStatus("Choose a report category.", "bad");
    els("questionFeedbackCategory")?.focus();
    return;
  }

  if (!payload.message) {
    setQuestionFeedbackStatus("Enter a short message about the issue.", "bad");
    els("questionFeedbackMessage")?.focus();
    return;
  }

  const submitBtn = els("btnSubmitQuestionFeedback");
  if (submitBtn) submitBtn.disabled = true;

  setQuestionFeedbackStatus("Submitting report…");

  try {
    const result = await submitQuestionFeedback(payload);
    if (!result?.ok) throw new Error(result?.error || "Question report failed.");

    resetQuestionFeedbackForm({ keepStatus: true });
    setQuestionFeedbackStatus("Thanks — this question report was submitted successfully.", "ok");
  } catch (error) {
    setQuestionFeedbackStatus(error?.message || "Unable to submit this report right now.", "bad");
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

async function handleCbtQuestionFeedbackSubmit(event) {
  if (event) event.preventDefault();

  const payload = getCbtQuestionFeedbackDraft();

  if (!payload.question_id) {
    setCbtQuestionFeedbackStatus("Open a CBT question before submitting a report.", "bad");
    return;
  }

  if (!payload.category) {
    setCbtQuestionFeedbackStatus("Choose a report category.", "bad");
    els("cbtQuestionFeedbackCategory")?.focus();
    return;
  }

  if (!payload.message) {
    setCbtQuestionFeedbackStatus("Enter a short message about the issue.", "bad");
    els("cbtQuestionFeedbackMessage")?.focus();
    return;
  }

  const submitBtn = els("btnSubmitCbtQuestionFeedback");
  if (submitBtn) submitBtn.disabled = true;

  setCbtQuestionFeedbackStatus("Submitting report…");

  try {
    const result = await submitQuestionFeedback(payload);
    if (!result?.ok) throw new Error(result?.error || "Question report failed.");

    resetCbtQuestionFeedbackForm({ keepStatus: true });
    setCbtQuestionFeedbackStatus("Thanks — this CBT question report was submitted successfully.", "ok");
  } catch (error) {
    setCbtQuestionFeedbackStatus(error?.message || "Unable to submit this CBT report right now.", "bad");
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

async function submitPlatformFeedback(payload) {
  const normalizedPayload = {
    category: String(payload?.category || "").trim(),
    message: String(payload?.message || "").trim(),
    source_area: String(payload?.source_area || "footer").trim() || "footer",
  };

  const response = await api("/feedback/platform", {
    method: "POST",
    body: JSON.stringify(normalizedPayload),
  });

  if (response?.ok === false) {
    return response;
  }

  state.lastFeedbackSubmission = {
    ...normalizedPayload,
    id: response?.id || "",
    submittedAt: new Date().toISOString(),
  };

  return {
    ok: true,
    id: response?.id || "",
  };
}

async function handleFeedbackSubmit(event) {
  if (event) event.preventDefault();

  const payload = getFeedbackDraft();
  if (!payload.category) {
    setFeedbackStatus("Choose a feedback category.", "bad");
    els("feedbackCategory")?.focus();
    return;
  }

  if (!payload.message) {
    setFeedbackStatus("Enter your feedback message.", "bad");
    els("feedbackMessage")?.focus();
    return;
  }

  const submitBtn = els("btnSubmitFeedback");
  if (submitBtn) submitBtn.disabled = true;

  setFeedbackStatus("Submitting feedback…");

  try {
    const result = await submitPlatformFeedback(payload);
    if (!result?.ok) throw new Error(result?.error || "Feedback submission failed.");

    setFeedbackStatus("Thanks — your feedback was submitted successfully.", "ok");
    const form = els("feedbackForm");
    if (form) form.reset();
  } catch (error) {
    setFeedbackStatus(error?.message || "Unable to submit feedback right now.", "bad");
  } finally {
    if (submitBtn) submitBtn.disabled = false;
  }
}

function setupSupportUi() {
  populateContactUi();
  populateFeedbackCategories();
  populateQuestionFeedbackCategories();
  initDiagramLightbox();

  const btnFooterContact = els("btnFooterContact");
  if (btnFooterContact) btnFooterContact.onclick = () => openSupportModal("contact");

  const btnFooterFeedback = els("btnFooterFeedback");
  if (btnFooterFeedback) btnFooterFeedback.onclick = () => {
    setFeedbackStatus("");
    openSupportModal("feedback");
  };

  const btnCloseContactModal = els("btnCloseContactModal");
  if (btnCloseContactModal) btnCloseContactModal.onclick = () => closeSupportModal("contact");

  const btnCloseFeedbackModal = els("btnCloseFeedbackModal");
  if (btnCloseFeedbackModal) btnCloseFeedbackModal.onclick = () => closeSupportModal("feedback");

  const feedbackForm = els("feedbackForm");
  if (feedbackForm) feedbackForm.onsubmit = handleFeedbackSubmit;

  ["contactUsModal", "feedbackModal"].forEach((id) => {
    const modal = els(id);
    if (!modal) return;

    modal.addEventListener("click", (event) => {
      if (event.target !== modal) return;
      closeSupportModal(id === "contactUsModal" ? "contact" : "feedback");
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;

    if (!els("feedbackModal")?.hidden) closeSupportModal("feedback");
    if (!els("contactUsModal")?.hidden) closeSupportModal("contact");
  });
}

function setPaidChip(paid) {
  state.isPaid = !!paid;
  const chip = els("chipPaid");
  if (!chip) return;

  if (state.isPaid) {
    chip.hidden = false;
    chip.style.removeProperty("display");
  } else {
    chip.hidden = true;
    chip.style.setProperty("display", "none", "important"); // force-hide
  }
}



function updatePracticeMetaUI() {
  const el = els("practiceMeta");
  if (!el) return;

  // If nothing is selected yet, keep the brand umbrella message
  const hasAny = !!(state.filters.exam || state.filters.year || state.filters.subject);
  if (!hasAny) {
    el.textContent = "NECO / WAEC / JAMB • Past Questions • Explanations";
    return;
  }

  const exam = state.filters.exam || "All Exams";
  const subject = state.filters.subject || "All Subjects";
  const year = state.filters.year || "All Years";
  el.textContent = `${exam} • ${subject} • ${year}`;
}



function saveApiBase() {
  const v = els("apiBase").value.trim();
  if (v) {
    state.apiBase = v.replace(/\/$/, "");
    localStorage.setItem("apiBase", state.apiBase);
  }
}

 function saveToken(t) {
  state.token = t || "";

  if (state.token) {
    sessionStorage.setItem("token", state.token); // ✅ session only
    localStorage.removeItem("token");             // cleanup old persistent token
  } else {
    sessionStorage.removeItem("token");
    localStorage.removeItem("token");
  }
}


// ====== Idle timeout (public/shared systems) ======
const IDLE_TIMEOUT_MS = 15 * 60 * 1000; // 15 minutes
let _idleTimer = null;

function stopIdleTimer() {
  if (_idleTimer) {
    clearTimeout(_idleTimer);
    _idleTimer = null;
  }
}

function resetIdleTimer() {
  // Only enforce idle timeout when authenticated
  if (!state.authenticated) return;
  stopIdleTimer();
  _idleTimer = setTimeout(async () => {
    // If user is still authenticated, expire session
    if (!state.authenticated) return;
    try {
      await doLogout();
    } catch {}
    setStatus("Session expired (idle). Please login again.", "bad");
  }, IDLE_TIMEOUT_MS);
}

function setupIdleTimeout() {
  const bump = () => resetIdleTimer();

  // Common user activity events
  ["click", "keydown", "mousemove", "touchstart", "scroll"].forEach((ev) => {
    window.addEventListener(ev, bump, { passive: true });
  });

  // If user comes back to the tab, refresh timer
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) bump();
  });

  // Start timer if already logged in (session restore)
  resetIdleTimer();
}
// ================================================


function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[c]));
}

function cleanPreviewText(s) {
  s = String(s || "");

  // Remove table placeholders
  s = s.replace(/\[\[table:[^\]]+\]\]/gi, "");

  // Remove diagram placeholders
  s = s.replace(/\[\[diagram:[^\]]+\]\]/gi, "");

  // Collapse extra blank lines/spaces
  s = s.replace(/\n\s*\n+/g, "\n");
  s = s.replace(/[ \t]+/g, " ");

  return s.trim();
}

function trimText(s, n = 140) {
  s = (s || "").trim();
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function isEmail(v) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v || "");
}

function renderSolutionSteps(steps, question = null) {
  if (!steps) return "";
  // steps can be string, array, or object
  if (typeof steps === "string") return `<div>${renderTextWithDiagrams(steps, { question, mode: "explain" })}</div>`;
  if (Array.isArray(steps)) {
    const items = steps
      .map((s) => {
        if (typeof s === "string") return `<li>${renderTextWithDiagrams(s, { question, mode: "explain" })}</li>`;
        // objects: show JSON safely
        return `<li>${escapeHtml(JSON.stringify(s))}</li>`;
      })
      .join("");
    return `<ol style="margin:6px 0 0 18px;">${items}</ol>`;
  }
  return `<pre style="white-space:pre-wrap;margin:6px 0 0;">${escapeHtml(JSON.stringify(steps, null, 2))}</pre>`;
}

function renderExplanation(explanationArray, question = null) {
  if (!Array.isArray(explanationArray)) {
    return renderTextWithDiagrams(String(explanationArray || ""), { question, mode: "explain" });
  }

  return `<div style="margin:6px 0 0; line-height:1.6;">
    ${explanationArray.map(item => {
      if (item.startsWith("Option")) {
        return `<p style="margin:6px 0;"><strong>${escapeHtml(item)}</strong></p>`;
      } else if (item.startsWith("Memory hook")) {
        return `<p style="margin:6px 0;"><em>${escapeHtml(item)}</em></p>`;
      } else {
        return `<p style="margin:6px 0;">${escapeHtml(item)}</p>`;
      }
    }).join("")}
  </div>`;
}

function renderSubQuestions(question, items, opts = {}) {
  const showAnswers = opts.showAnswers !== false;              // default true
  const showExplanations = opts.showExplanations !== false;    // default true
  const showDiagrams = opts.showDiagrams !== false;            // default true

  // mode: "question" | "reveal" | "explain"
  const tables = opts.tables || question?.tables || {};
  const mode = opts.mode || "question";

  if (!items) return "";
  if (!Array.isArray(items)) {
    return `<pre style="white-space:pre-wrap;margin:6px 0 0;">${escapeHtml(JSON.stringify(items, null, 2))}</pre>`;
  }

  const renderNode = (n) => {
    if (!n || typeof n !== "object") return "";

    const label = n.label ? `<b>${escapeHtml(String(n.label))}</b> ` : "";

    const subqText = n.question_text || n.text || "";
    const text = subqText
      ? `${renderTextWithDiagrams(String(subqText), { question, tables, mode })}`
      : "";

    // Subquestion diagrams (question-phase diagrams)
    const qDiagrams = (showDiagrams && Array.isArray(n.diagrams) && n.diagrams.length)
      ? renderDiagramsHtml(n.diagrams)
      : "";

    const answer = (showAnswers && n.answer)
      ? `<div style="margin-top:8px;"><b>Answer:</b> ${renderTextWithDiagrams(String(n.answer), { question, tables, mode: "reveal" })}</div>`
      : "";

    // Answer diagrams (Reveal)
    const aDiagrams = (showAnswers && showDiagrams && Array.isArray(n.answer_diagrams) && n.answer_diagrams.length)
      ? renderDiagramsHtml(n.answer_diagrams)
      : "";

    const explanation = (showExplanations && n.explanation)
      ? `<div style="margin-top:8px;">
          <b>Explanation:</b>
         <div style="margin-top:6px;">
         ${renderExplanation(n.explanation, question)}
         </div>
        </div>`
      : "";

    // Explanation diagrams (Explain)
    const eDiagrams = (showExplanations && showDiagrams && Array.isArray(n.explanation_diagrams) && n.explanation_diagrams.length)
      ? renderDiagramsHtml(n.explanation_diagrams)
      : "";

    // Children (nested)
    const children = Array.isArray(n.children) && n.children.length
      ? `<div style="margin-top:12px;padding-left:10px;border-left:2px solid rgba(0,0,0,0.08);">
           ${n.children.map(renderNode).join("")}
         </div>`
      : "";

    return `
      <div style="margin:10px 0; padding:10px; border:1px solid rgba(0,0,0,0.08); border-radius:12px;">
        <div>${label}${text}</div>
        ${qDiagrams}
        ${answer}
        ${aDiagrams}
        ${explanation}
        ${eDiagrams}
        ${children}
      </div>
    `;
  };

  return items.map(renderNode).join("");
}


// ---- Viewer rendering section ----
function getPassageDisplayHtml(passageSnapshot) {
  if (!passageSnapshot) return "";

  if (typeof passageSnapshot === "string") {
    const trimmed = passageSnapshot.trim();
    if (!trimmed) return "";
    return `<div class="passage-body">${renderTextWithDiagrams(trimmed, { mode: "question" })}</div>`;
  }

  if (typeof passageSnapshot !== "object") return "";

  const title = String(passageSnapshot.title || passageSnapshot.heading || passageSnapshot.label || "Passage").trim();
  const passageText = String(
    passageSnapshot.passage_text
    || passageSnapshot.text
    || passageSnapshot.content
    || passageSnapshot.body
    || ""
  ).trim();

  const metaBits = [];
  if (passageSnapshot.passage_type) metaBits.push(String(passageSnapshot.passage_type));
  if (passageSnapshot.reference) metaBits.push(String(passageSnapshot.reference));

  const titleHtml = `<div class="passage-title">${escapeHtml(title || "Passage")}</div>`;
  const metaHtml = metaBits.length ? `<div class="passage-meta">${escapeHtml(metaBits.join(" • "))}</div>` : "";
  const bodyHtml = passageText
    ? `<div class="passage-body">${renderTextWithDiagrams(passageText, { mode: "question" })}</div>`
    : "";

  if (!bodyHtml && !metaHtml && !title) return "";
  return `${titleHtml}${metaHtml}${bodyHtml}`;
}

function renderQuestion(question) {
  const qPassageEl = els("qPassage");
  const passageHtml = getPassageDisplayHtml(question.passage_snapshot);
  if (qPassageEl) {
    qPassageEl.hidden = !passageHtml;
    qPassageEl.innerHTML = passageHtml;
  }

  // Question text
  const qTextEl = els("qText");
  const hasInlineTableRef = /\[\[table:[A-Za-z0-9_]+\]\]/.test(question.question_text || "");

  if (qTextEl) {
    qTextEl.innerHTML = `<div>${renderTextWithDiagrams(question.question_text || "", { question, tables: question.tables || {}, mode: "question" })}</div>`;
  }

  // Render question tables only when they are NOT already embedded inline
  const qTablesEl = els("qTables");
  if (qTablesEl) {
    if (hasInlineTableRef) {
      qTablesEl.innerHTML = "";
      qTablesEl.hidden = true;
    } else {
      qTablesEl.hidden = false;
      renderTablesInto(qTablesEl, question.tables || {}, question.table_refs || null, "question");
    }
  }

  // Main question diagrams (separate field)
  renderDiagramsInto(els("qDiagrams"), question.diagrams || [], { variant: "block" });

  // Sub-questions (question-only view; answers hidden until reveal/explain)
  const subBox = els("qSubQuestions");
  if (subBox) {
    if (question.sub_questions && Array.isArray(question.sub_questions) && question.sub_questions.length) {
      subBox.hidden = false;
      subBox.innerHTML = `
        <div style="font-weight:700; margin:12px 0 6px;">Sub-questions</div>
        ${renderSubQuestions(question, question.sub_questions, { showAnswers: false, showExplanations: false, showDiagrams: true })}
      `;
    } else {
      subBox.hidden = true;
      subBox.innerHTML = "";
    }
  }

  // Reset explanation box
  const exp = els("qExplain");
  if (exp) {
    exp.hidden = true;
    exp.innerHTML = "";
  }
}

function renderQuestionInto(prefix, question, { selectedOptionKey = null, onOptionSelect = null, readOnly = false } = {}) {
  const passageEl = els(`${prefix}Passage`);
  const textEl = els(`${prefix}QuestionText`);
  const tablesEl = els(`${prefix}QuestionTables`);
  const diagramsEl = els(`${prefix}QuestionDiagrams`);
  const subQuestionsEl = els(`${prefix}SubQuestions`);
  const optionsEl = els(`${prefix}Options`);

  const passageHtml = getPassageDisplayHtml(question.passage_snapshot);
  if (passageEl) {
    passageEl.hidden = !passageHtml;
    passageEl.innerHTML = passageHtml;
  }

  const hasInlineTableRef = /\[\[table:[A-Za-z0-9_]+\]\]/.test(question.question_text || "");

  if (textEl) {
    textEl.innerHTML = `<div>${renderTextWithDiagrams(question.question_text || "", { question, tables: question.tables || {}, mode: "question" })}</div>`;
  }

  if (tablesEl) {
    if (hasInlineTableRef) {
      tablesEl.innerHTML = "";
      tablesEl.hidden = true;
    } else {
      tablesEl.hidden = false;
      renderTablesInto(tablesEl, question.tables || {}, question.table_refs || null, "question");
    }
  }

  renderDiagramsInto(diagramsEl, question.diagrams || [], { variant: "block" });

  if (subQuestionsEl) {
    if (question.sub_questions && Array.isArray(question.sub_questions) && question.sub_questions.length) {
      subQuestionsEl.hidden = false;
      subQuestionsEl.innerHTML = `
        <div style="font-weight:700; margin:12px 0 6px;">Sub-questions</div>
        ${renderSubQuestions(question, question.sub_questions, { showAnswers: false, showExplanations: false, showDiagrams: true })}
      `;
    } else {
      subQuestionsEl.hidden = true;
      subQuestionsEl.innerHTML = "";
    }
  }

  if (optionsEl) {
    optionsEl.innerHTML = "";
    const options = question.options && typeof question.options === "object" ? question.options : null;
    if (options) {
      for (const key of Object.keys(options)) {
        const optionEl = document.createElement("div");
        optionEl.className = "opt";
        if (selectedOptionKey === key) optionEl.classList.add("selected");
        optionEl.dataset.key = key;
        optionEl.innerHTML = `<b>${escapeHtml(key)}</b>. ${escapeHtml(options[key])}`;
        if (!readOnly) {
          optionEl.onclick = () => {
            const nextKey = selectedOptionKey === key ? null : key;
            if (typeof onOptionSelect === "function") {
              onOptionSelect(nextKey, question, key);
            }
          };
        } else {
          optionEl.setAttribute("aria-disabled", "true");
          optionEl.classList.add("disabled");
        }
        optionsEl.appendChild(optionEl);
      }
    }
  }
}

function renderAnswerBlock(question) {
  const parts = [];

  const mainAns = question.answer ? renderTextWithDiagrams(String(question.answer), { question, tables: question.tables, mode: "reveal" }) : "—";
  parts.push(`<div><b>Answer:</b> ${mainAns}</div>`);

  // Optional answer diagrams
  if (Array.isArray(question.answer_diagrams) && question.answer_diagrams.length) {
    parts.push(renderDiagramsHtml(question.answer_diagrams));
  }

  // Sub-question answers (if present)
  if (question.sub_questions) {
    const html = renderSubQuestions(question, question.sub_questions, { showAnswers: true, showExplanations: false, showDiagrams: true, mode: "reveal" });
    if (html) parts.push(`<div style="margin-top:12px;"><b>Sub-question answers:</b>${html}</div>`);
  }

  return parts.join("<hr/>");
}

function updateCbtSetupMeta() {
  const metaEl = els("cbtSetupMeta");
  if (!metaEl) return;

  const bits = [
    state.filters.exam || "Exam not selected",
    state.filters.year || "Year not selected",
    state.filters.subject || "Subject not selected",
  ];
  metaEl.textContent = bits.join(" • ");
}

const CBT_DEFAULT_SECONDS_PER_QUESTION = 60;

function getCbtQuestionKey(question, fallbackIndex = state.cbt.currentIndex) {
  if (!question) return `cbt-${fallbackIndex}`;
  return question.id || question.qid || question._id || `cbt-${fallbackIndex}`;
}

function getCbtSelectedAnswer(question, fallbackIndex = state.cbt.currentIndex) {
  const key = getCbtQuestionKey(question, fallbackIndex);
  return state.cbt.answersByQuestionKey[key] ?? null;
}

function normalizeCbtAnswerKey(value) {
  const raw = String(value ?? "").trim();
  if (!raw) return "";
  const match = raw.match(/^([A-Z])/i);
  return match ? match[1].toUpperCase() : raw.toUpperCase();
}

function calculateCbtResult() {
  const questions = Array.isArray(state.cbt.questions) ? state.cbt.questions : [];
  const totalQuestions = questions.length;
  let answeredQuestions = 0;
  let correctAnswers = 0;

  for (let index = 0; index < questions.length; index += 1) {
    const question = questions[index];
    const selected = normalizeCbtAnswerKey(getCbtSelectedAnswer(question, index));
    const expected = normalizeCbtAnswerKey(question?.answer);
    if (selected) answeredQuestions += 1;
    if (selected && expected && selected === expected) correctAnswers += 1;
  }

  const wrongAnswers = Math.max(0, answeredQuestions - correctAnswers);
  const percentage = totalQuestions ? Math.round((correctAnswers / totalQuestions) * 100) : 0;

  return {
    totalQuestions,
    answeredQuestions,
    correctAnswers,
    wrongAnswers,
    score: correctAnswers,
    percentage,
    submittedAt: Date.now(),
  };
}

function renderCbtResult() {
  const result = state.cbt.result;
  const resultSection = els("resultSection");
  if (!result || !resultSection) return;

  const totalEl = els("resultTotalQuestions");
  const answeredEl = els("resultAnsweredQuestions");
  const correctEl = els("resultCorrectAnswers");
  const wrongEl = els("resultWrongAnswers");
  const badgeEl = els("resultScoreBadge");
  const metaEl = els("resultSummaryMeta");
  const statusEl = els("resultStatus");

  if (totalEl) totalEl.textContent = String(result.totalQuestions);
  if (answeredEl) answeredEl.textContent = String(result.answeredQuestions);
  if (correctEl) correctEl.textContent = String(result.correctAnswers);
  if (wrongEl) wrongEl.textContent = String(result.wrongAnswers);
  if (badgeEl) badgeEl.textContent = `Score ${result.score} / ${result.totalQuestions} (${result.percentage}%)`;

  const filterBits = [state.filters.exam, state.filters.year, state.filters.subject].filter(Boolean);
  if (metaEl) metaEl.textContent = filterBits.length
    ? `Submitted ${filterBits.join(" • ")} CBT session.`
    : "Submitted CBT session.";

  if (statusEl) {
    const unanswered = Math.max(0, result.totalQuestions - result.answeredQuestions);
    statusEl.textContent = unanswered
      ? `${unanswered} question(s) were left unanswered before submission.`
      : "All questions were answered before submission.";
  }

  resultSection.hidden = false;
}

function submitCurrentCbtSession({ reason = "manual" } = {}) {
  if (!state.cbt.sessionReady || state.cbt.submitted) return;

  resetCbtQuestionFeedbackForm();
  setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
  stopCbtTimer();
  syncCbtTimer();
  state.cbt.submitted = true;
  state.cbt.result = calculateCbtResult();
  state.cbt.sessionReady = false;

  renderCbtResult();
  renderCbtQuestion();
  updateCbtNavButtons();

  const answered = state.cbt.result?.answeredQuestions ?? 0;
  const total = state.cbt.result?.totalQuestions ?? 0;
  const msg = reason === "timer"
    ? `Time is up. CBT submitted automatically with ${answered} of ${total} question(s) answered.`
    : `CBT submitted. You answered ${answered} of ${total} question(s).`;
  setCbtStatus(msg, reason === "timer" ? "bad" : "ok");

  els("cbtWorkspace")?.setAttribute("hidden", "hidden");
  els("resultSection")?.removeAttribute("hidden");
  els("resultSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setCbtSelectedAnswer(question, answerKey, fallbackIndex = state.cbt.currentIndex) {
  const key = getCbtQuestionKey(question, fallbackIndex);
  if (!key) return;
  if (answerKey == null) {
    delete state.cbt.answersByQuestionKey[key];
  } else {
    state.cbt.answersByQuestionKey[key] = answerKey;
  }
  state.cbt.selectedOptionKey = answerKey;
  updateCbtSessionMeta();
}

function formatCountdown(ms) {
  const totalSeconds = Math.max(0, Math.ceil(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function stopCbtTimer() {
  if (state.cbt.timerIntervalId) {
    clearInterval(state.cbt.timerIntervalId);
    state.cbt.timerIntervalId = null;
  }
}

function updateCbtTimerUi() {
  const timerEl = els("cbtTimer");
  if (!timerEl) return;

  timerEl.textContent = formatCountdown(state.cbt.timeRemainingMs);
  timerEl.classList.toggle("is-warning", state.cbt.timeRemainingMs > 0 && state.cbt.timeRemainingMs <= 5 * 60 * 1000);
  timerEl.classList.toggle("is-expired", state.cbt.timeRemainingMs <= 0);
}

function updateCbtSessionMeta() {
  const sessionMetaEl = els("cbtSessionMeta");
  const candidateMetaEl = els("cbtCandidateMeta");
  const total = state.cbt.questions.length;
  const answered = Object.keys(state.cbt.answersByQuestionKey).length;
  const remaining = Math.max(0, total - answered);
  const bits = [];

  if (total > 0) bits.push(`${answered} answered`);
  if (total > 0) bits.push(`${remaining} remaining`);
  if (state.cbt.submitted) bits.push("Submitted");
  else if (state.cbt.timerStartedAt) bits.push(`Time left ${formatCountdown(state.cbt.timeRemainingMs)}`);
  if (state.cbt.timerExpired) bits.push("Time elapsed");

  const metaText = bits.join(" • ");
  if (sessionMetaEl) sessionMetaEl.textContent = metaText;
  if (candidateMetaEl) {
    candidateMetaEl.textContent = total
      ? `Answers save inside this CBT session only. ${metaText || "Session ready."}`
      : "Start a CBT session to begin the timer and save answers as you move.";
  }
}

function syncCbtTimer() {
  if (!state.cbt.timerStartedAt || !state.cbt.timerDurationMs) {
    state.cbt.timeRemainingMs = 0;
    updateCbtTimerUi();
    updateCbtSessionMeta();
    return;
  }

  const elapsed = Date.now() - state.cbt.timerStartedAt;
  state.cbt.timeRemainingMs = Math.max(0, state.cbt.timerDurationMs - elapsed);
  const justExpired = state.cbt.timeRemainingMs === 0 && !state.cbt.timerExpired;
  state.cbt.timerExpired = state.cbt.timeRemainingMs === 0;

  if (justExpired) {
    submitCurrentCbtSession({ reason: "timer" });
    return;
  }

  updateCbtTimerUi();
  updateCbtSessionMeta();
}

function startCbtTimer(questionCount = 0) {
  stopCbtTimer();
  const durationMs = Math.max(1, questionCount) * CBT_DEFAULT_SECONDS_PER_QUESTION * 1000;
  state.cbt.timerDurationMs = durationMs;
  state.cbt.timeRemainingMs = durationMs;
  state.cbt.timerStartedAt = Date.now();
  state.cbt.timerExpired = false;
  syncCbtTimer();
  state.cbt.timerIntervalId = setInterval(syncCbtTimer, 1000);
}

function updateCbtNavButtons() {
  const prevBtn = els("btnCbtPrev");
  const nextBtn = els("btnCbtNext");
  const submitBtn = els("btnCbtSubmit");
  const total = state.cbt.questions.length;
  const canReview = state.cbt.submitted && total > 0;
  const navLocked = state.cbt.loading || total === 0 || (!state.cbt.sessionReady && !canReview);
  if (prevBtn) prevBtn.disabled = navLocked || state.cbt.currentIndex <= 0;
  if (nextBtn) nextBtn.disabled = navLocked || state.cbt.currentIndex >= total - 1;
  if (submitBtn) submitBtn.disabled = state.cbt.loading || !state.cbt.sessionReady || state.cbt.submitted || total === 0;
}

function renderCbtQuestion() {
  const workspace = els("cbtWorkspace");
  const titleEl = els("cbtQuestionTitle");
  const positionEl = els("cbtQuestionPosition");
  const metaEl = els("cbtQuestionMeta");
  const cbtSection = els("cbtSection");

  if (cbtSection) cbtSection.hidden = false;

  const total = state.cbt.questions.length;
  const question = total ? state.cbt.questions[state.cbt.currentIndex] : null;

  if (!workspace || !titleEl || !positionEl || !metaEl) return;

  if (!question) {
    workspace.hidden = true;
    setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
    resetCbtQuestionFeedbackForm();
    titleEl.textContent = "CBT Question";
    positionEl.textContent = "Question 0 of 0";
    metaEl.textContent = "";
    updateCbtTimerUi();
    updateCbtSessionMeta();
    updateCbtNavButtons();
    return;
  }

  const isReviewMode = !!state.cbt.submitted;
  workspace.hidden = false;
  titleEl.textContent = question.id || `Question ${state.cbt.currentIndex + 1}`;
  positionEl.textContent = `Question ${state.cbt.currentIndex + 1} of ${total}`;

  const meta = [];
  if (question.type) meta.push(question.type);
  if (question.paper) meta.push(question.paper);
  if (question.section && question.type !== "objective") meta.push(question.section);
  if (question.marks) meta.push(`${question.marks} marks`);
  if (question.page) meta.push(`page ${question.page}`);
  if (question.exam) meta.push(question.exam);
  if (question.year) meta.push(String(question.year));
  if (question.subject) meta.push(question.subject);
  metaEl.textContent = meta.join(" • ");

  state.cbt.selectedOptionKey = getCbtSelectedAnswer(question, state.cbt.currentIndex);
  if (!state.cbt.feedbackOpen || isReviewMode) {
    resetCbtQuestionFeedbackForm();
    setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
  }
  renderQuestionInto("cbt", question, {
    selectedOptionKey: state.cbt.selectedOptionKey,
    readOnly: isReviewMode,
    onOptionSelect: (nextKey, activeQuestion) => {
      if (isReviewMode) return;
      setCbtSelectedAnswer(activeQuestion, nextKey, state.cbt.currentIndex);
      renderCbtQuestion();
    },
  });
  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateCbtNavButtons();

  const reportBtn = els("btnCbtReportQuestion");
  if (reportBtn) reportBtn.disabled = isReviewMode;

  const statusBits = [];
  if (isReviewMode) statusBits.push("Review mode — answers are locked after submission.");
  if (state.cbt.result) statusBits.push(`Current score ${state.cbt.result.score}/${state.cbt.result.totalQuestions}.`);
  if (statusBits.length) setCbtStatus(statusBits.join(" "), isReviewMode ? "ok" : "");
}

async function loadCbtSession() {
  updateCbtSetupMeta();

  if (!filtersReady()) {
    els("cbtSection").hidden = false;
    els("cbtWorkspace").hidden = true;
    setCbtStatus("Choose Exam, Year, and Subject in Filters before starting CBT.", "bad");
    return;
  }

  state.cbt.loading = true;
  state.cbt.questions = [];
  state.cbt.currentIndex = 0;
  state.cbt.selectedOptionKey = null;
  state.cbt.answersByQuestionKey = {};
  state.cbt.sessionReady = false;
  state.cbt.submitted = false;
  state.cbt.result = null;
  state.cbt.timerDurationMs = 0;
  state.cbt.timeRemainingMs = 0;
  state.cbt.timerStartedAt = 0;
  state.cbt.timerExpired = false;
  state.cbt.feedbackOpen = false;
  stopCbtTimer();
  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateCbtNavButtons();
  els("resultSection")?.setAttribute("hidden", "hidden");
  setCbtStatus("Loading CBT questions…", "ok");

  const response = await fetchQuestionPage({
    mode: "objective",
    limit: state.pageSize,
    offset: 0,
    exam: state.filters.exam,
    year: state.filters.year,
    subject: state.filters.subject,
  });

  state.cbt.loading = false;

  if (!response?.ok) {
    els("cbtSection").hidden = false;
    els("cbtWorkspace").hidden = true;
    setCbtStatus(`Failed to load CBT questions: ${response?.error || "unknown error"}`, "bad");
    updateCbtNavButtons();
    return;
  }

  const items = Array.isArray(response.items) ? response.items : [];
  state.cbt.questions = items.filter((item) => String(item.type || "").toLowerCase() === "objective");
  state.cbt.currentIndex = 0;
  state.cbt.selectedOptionKey = null;
  state.cbt.answersByQuestionKey = {};
  state.cbt.submitted = false;
  state.cbt.result = null;
  state.cbt.sessionReady = state.cbt.questions.length > 0;

  if (!state.cbt.sessionReady) {
    els("cbtSection").hidden = false;
    els("cbtWorkspace").hidden = true;
    setCbtStatus("No objective CBT questions matched the selected filters yet.", "bad");
    updateCbtNavButtons();
    return;
  }

  startCbtTimer(state.cbt.questions.length);
  setCbtStatus(`Loaded ${state.cbt.questions.length} CBT question(s). Timer started.`, "ok");
  renderCbtQuestion();
}

function moveCbtQuestion(step) {
  const total = state.cbt.questions.length;
  if (!total) return;
  const nextIndex = Math.max(0, Math.min(total - 1, state.cbt.currentIndex + step));
  if (nextIndex === state.cbt.currentIndex) return;
  resetCbtQuestionFeedbackForm();
  setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
  state.cbt.currentIndex = nextIndex;
  state.cbt.selectedOptionKey = getCbtSelectedAnswer(state.cbt.questions[nextIndex], nextIndex);
  renderCbtQuestion();
}

function renderExplainBlock(question) {
  const parts = [];

  if (question.explanation) {
    let explanationHtml = "";

    if (Array.isArray(question.explanation)) {
      explanationHtml = renderExplanation(question.explanation, question);
    } else {
      explanationHtml = renderTextWithDiagrams(String(question.explanation), {
        question,
        tables: question.tables,
        mode: "explain"
      });
    }

    parts.push(
      `<div><b>Explanation:</b>
         <div style="margin-top:6px;">
           ${explanationHtml}
         </div>
       </div>`
    );
   }

  if (Array.isArray(question.explanation_diagrams) && question.explanation_diagrams.length) {
    parts.push(renderDiagramsHtml(question.explanation_diagrams));
  }

  if (question.solution_steps) {
    parts.push(`<div><b>Steps:</b>${renderSolutionSteps(question.solution_steps, question)}</div>`);
  }

  if (question.sub_questions) {
    parts.push(`<div><b>Sub-questions:</b>${renderSubQuestions(question, question.sub_questions, { showAnswers: true, showExplanations: true, showDiagrams: true, mode: "explain" })}</div>`);
  }

  return parts.length ? parts.join("<hr/>") : `<div>No explanation/steps available.</div>`;
}


async function api(path, opts = {}) {
  // ✅ keep token consistent across tabs
  state.token = sessionStorage.getItem("token") || "";

  const url = `${state.apiBase.replace(/\/$/, "")}${path}`;
  const headers = opts.headers ? { ...opts.headers } : {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (!headers["Content-Type"] && opts.method && opts.method !== "GET") {
    headers["Content-Type"] = "application/json";
  }

 let res;
 try {
  res = await fetch(url, { ...opts, headers });
 } catch (e) {
  return { ok: false, status: 0, error: "Network error: cannot reach backend (CORS/down/wrong URL)" };
 }

  const ct = res.headers.get("content-type") || "";

  let body = null;
  if (ct.includes("application/json")) body = await res.json().catch(() => null);
  else body = await res.text().catch(() => null);

  if (!res.ok) {
    return { ok: false, status: res.status, error: body?.detail || body || "Request failed" };
  }
  return body || { ok: true };
}

// ====== Filters ======
function fillSelect(el, values) {
  el.innerHTML = "";
  for (const v of values) {
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v === "" ? "All" : v;
    el.appendChild(opt);
  }
}

function _safeSetSelectValue(sel, value) {
  if (!sel) return;
  const v = value || "";
  const exists = Array.from(sel.options).some((o) => o.value === v);
  sel.value = exists ? v : "";
}
function saveFilterCache(data) {
  try {
    localStorage.setItem(
      FILTER_CACHE_KEY,
      JSON.stringify({ ts: Date.now(), data })
    );
  } catch {}
}

function loadFilterCache() {
  try {
    const raw = localStorage.getItem(FILTER_CACHE_KEY);
    if (!raw) return null;

    const parsed = JSON.parse(raw);
    if (!parsed?.ts || !parsed?.data) return null;

    if (Date.now() - parsed.ts > FILTER_CACHE_TTL_MS) return null;
    return parsed.data;
  } catch {
    return null;
  }
}


async function fetchFilters({ qtype = null, exam = null, year = null } = {}) {
  const params = new URLSearchParams();
  if (qtype) params.set("qtype", qtype);
  if (exam) params.set("exam", exam);
  if (year !== null && year !== undefined && year !== "") {
    params.set("year", String(year));
  }

  const qs = params.toString();
  const path = `/filters${qs ? `?${qs}` : ""}`;
  return api(path, { method: "GET" });
}

async function fetchQuestionPage({ mode, limit, offset, exam, year, subject } = {}) {
  const params = new URLSearchParams();
  if (limit !== null && limit !== undefined && limit !== "") params.set("limit", String(limit));
  if (offset !== null && offset !== undefined && offset !== "") params.set("offset", String(offset));
  if (exam) params.set("exam", exam);
  if (year) params.set("year", year);
  if (subject) params.set("subject", subject);

  const qs = params.toString();
  return api(`/questions/${mode}?${qs}`);
}

async function fetchQuestion(id) {
  return api(`/question/${encodeURIComponent(id)}`);
}

async function fetchAdminQuestions({ limit = 200, offset = 0, exam = "", year = "", subject = "", qtype = "" } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (exam) params.set("exam", exam);
  if (year) params.set("year", String(year));
  if (subject) params.set("subject", subject);
  if (qtype) params.set("qtype", qtype);
  return api(`/admin/questions?${params.toString()}`);
}

async function fetchAdminFeedback({ limit = 200, offset = 0, feedbackType = "", sourceArea = "" } = {}) {
  const params = new URLSearchParams();
  params.set("limit", String(limit));
  params.set("offset", String(offset));
  if (feedbackType) params.set("feedback_type", feedbackType);
  if (sourceArea) params.set("source_area", sourceArea);
  return api(`/admin/feedback?${params.toString()}`);
}


async function refreshFilterOptions({ exam, year, qtype, keepSelection = true } = {}) {
  const examSel = els("examFilter");
  const yearSel = els("yearFilter");
  const subjSel = els("subjectFilter");
  if (!examSel || !yearSel || !subjSel) return;

  const prev = keepSelection
    ? { exam: examSel.value, year: yearSel.value, subject: subjSel.value }
    : { exam: "", year: "", subject: "" };

  const mode = els("mode")?.value || "objective";
  const qtypeParam = qtype || mode || null;

  let data = null;
  try {
    const response = await fetchFilters({
      qtype: qtypeParam,
      exam: exam ?? prev.exam ?? null,
      year: year ?? (prev.year ? parseInt(prev.year, 10) : null),
    });

    if (response?.ok && Array.isArray(response.exams) && Array.isArray(response.years) && Array.isArray(response.subjects)) {
      saveFilterCache(response);
      data = response;
    }
  } catch (e) {
    console.warn("Filters API failed:", e);
  }

  if (!data) {
    const cached = loadFilterCache();
    if (cached) {
      console.warn("Using cached filters");
      data = cached;
    }
  }

  // ✅ Production behavior: no hardcoded fallbacks
  if (!data || !Array.isArray(data.exams) || !Array.isArray(data.years) || !Array.isArray(data.subjects)) {
    setStatus("Unable to load filters right now. Please check connection and retry.", "bad");
    // Keep existing selections as-is (don't wipe UI)
    return;
  }

  // Add empty option at top
  const exams = ["", ...data.exams.map(String)];
  const years = ["", ...data.years.map((y) => String(y))];
  const subjects = ["", ...data.subjects.map(String)];

  fillSelect(examSel, exams);
  fillSelect(yearSel, years);
  fillSelect(subjSel, subjects);

  // Restore previous selection if still valid, else keep first available
  _safeSetSelectValue(examSel, prev.exam);
  _safeSetSelectValue(yearSel, prev.year);
  _safeSetSelectValue(subjSel, prev.subject);
}


async function initFiltersUI() {
  const examSel = els("examFilter");
  const yearSel = els("yearFilter");
  const subjSel = els("subjectFilter");
  if (!examSel || !yearSel || !subjSel) return;

  // Load options from backend (/filters). Falls back if unavailable.
  await refreshFilterOptions({ keepSelection: true });

  // Restore saved selection (after options are loaded)
  examSel.value = state.filters.exam || examSel.value || "";
  yearSel.value = state.filters.year || yearSel.value || "";
  subjSel.value = state.filters.subject || subjSel.value || "";

  const save = () => {
    state.filters.exam = examSel.value || "";
    state.filters.year = yearSel.value || "";
    state.filters.subject = subjSel.value || "";
    localStorage.setItem("filter_exam", state.filters.exam);
    localStorage.setItem("filter_year", state.filters.year);
    localStorage.setItem("filter_subject", state.filters.subject);
  };

  examSel.onchange = async () => {
    save();
    await refreshFilterOptions({ exam: state.filters.exam || undefined, keepSelection: true });
    updatePracticeMetaUI();
    updateCbtSetupMeta();
    maybeAutoLoadAfterFilterChange();
  };

  yearSel.onchange = async () => {
    save();
    await refreshFilterOptions({
      exam: state.filters.exam || undefined,
      year: state.filters.year ? parseInt(state.filters.year, 10) : undefined,
      keepSelection: true
    });
    updatePracticeMetaUI();
    updateCbtSetupMeta();
    maybeAutoLoadAfterFilterChange();
  };

  subjSel.onchange = () => {
    save();
    updatePracticeMetaUI();
    updateCbtSetupMeta();
    maybeAutoLoadAfterFilterChange();
  };

  const btnClear = els("btnClearFilters");
  if (btnClear) {
    btnClear.onclick = async () => {
      examSel.value = "";
      yearSel.value = "";
      subjSel.value = "";
      save();
      await refreshFilterOptions({ keepSelection: true });
      updatePracticeMetaUI();
      updateCbtSetupMeta();
      if (isFirstTimeUser()) setStartGateVisible(true);
    };
  }
}


// ====== First-time gate + list pager ======
function filtersReady() {
  // Require these three so first-time users don't load "everything"
  return !!(state.filters.exam && state.filters.year && state.filters.subject);
}

function isFirstTimeUser() {
  const started = localStorage.getItem("ep_started") === "1";
  const hasAnySaved = !!(state.filters.exam || state.filters.year || state.filters.subject);
  return !started && !hasAnySaved;
}

function openFiltersPanel() {
  const fp = els("filtersPanel");
  if (fp) fp.open = true;
}

function setStartGateVisible(visible) {
  const gate = els("startGate");
  if (!gate) return;

  gate.hidden = !visible;

  if (visible) {
  // Ensure paywall never appears under the start gate
  state.paywalled = false;
  const pw = els("paywall");
  if (pw) pw.classList.remove("is-open");

  // Open filters panel
  openFiltersPanel();

  // 🎯 Step 4E: auto-focus Exam filter for first-time users
  requestAnimationFrame(() => {
    const examSel = els("examFilter");
    if (examSel) examSel.focus();
  });
}

}


function setListPagerUI({ loading = false } = {}) {
  const prev = els("btnPrevPage");
  const next = els("btnNextPage");
  const label = els("pageLabel");
  const hint = els("pageHint");

  if (label) label.textContent = `Page ${state.pageIndex + 1}`;

  if (hint) {
    if (state.paywalled) hint.textContent = " • Upgrade to continue";
    else if (state.endReached) hint.textContent = " • End reached";
    else hint.textContent = "";
  }

  if (prev) prev.disabled = loading || state.pageIndex <= 0;
  if (next) next.disabled = loading || state.paywalled || state.endReached;
}

function maybeAutoLoadAfterFilterChange() {
  // First-time users: once filters are ready, load page 1 automatically
  if (filtersReady()) {
    setStartGateVisible(false);
    state.pageIndex = 0;
    state.endReached = false;
    state.paywalled = false;
    loadList(0);
  } else {
    if (isFirstTimeUser()) setStartGateVisible(true);
  }
}

// ====== List ======
function renderList(items) {
  const list = els("list");
  list.innerHTML = "";

  currentListIds = (items || []).map(x => x.id).filter(Boolean);
  if (DEBUG_QUESTIONS) {
    console.debug("[questions] renderList received items.length", Array.isArray(items) ? items.length : 0);
    console.debug("[questions] renderList currentListIds.length", currentListIds.length);
  }

  if (!items || !items.length) {
    list.innerHTML = `<div class="status">No items returned. Try a smaller offset or clear filters.</div>`;
    if (DEBUG_QUESTIONS) {
      console.debug("[questions] renderList #list child count after empty state", list.childElementCount);
    }
    return;
  }

  for (const q of items) {
     const div = document.createElement("div");
     div.className = "item";
     div.dataset.qid = q.id;
     div.onclick = () => openQuestion(q.id);


    const meta = [];
    if (q.type) meta.push(q.type);
    if (q.paper) meta.push(q.paper);
    if (q.section && q.type !== "objective") meta.push(q.section);
    if (q.marks) meta.push(`${q.marks} marks`);
    if (q.page) meta.push(`page ${q.page}`);

    const tag = [];
    if (q.exam) tag.push(q.exam);
    if (q.year) tag.push(String(q.year));
    if (q.subject) tag.push(q.subject);
    if (tag.length) meta.push(tag.join(" "));

    div.innerHTML = `
       <div class="card-top">
         <span class="qid">${escapeHtml(q.id || "")}</span>
         ${q.type ? `<span class="pill">${escapeHtml(q.type)}</span>` : ""}
       </div>

      <div class="qtext">${escapeHtml(trimText(cleanPreviewText(q.question_text), 140))}</div>

  <div class="meta">${escapeHtml(meta.join(" • "))}</div>
`;


    list.appendChild(div);
  }

  if (DEBUG_QUESTIONS) {
    console.debug("[questions] renderList #list child count after render", list.childElementCount);
  }

  // restore highlight + visibility if a question is already selected
  if (activeQuestionId && currentListIds.includes(activeQuestionId)) {
    highlightQuestionCard(activeQuestionId);
    requestAnimationFrame(() => ensureActiveCardVisibleInList(activeQuestionId));
  } else if (activeQuestionId) {
    closeViewer();
  }
}


async function openQuestion(id) {
  const requestSeq = ++practiceOpenRequestSeq;

  try {
    activeQuestionId = id;

    syncCurrentIndexFromId(id);
    updatePrevNextButtons();
    clearOptionSelection();

    // Reset explanation state for new question
    const exp = els("qExplain");
    if (exp) {
      exp.hidden = true;
      exp.innerHTML = "";
    }

    highlightQuestionCard(id);

    // ✅ open viewer context first (this changes list max-height)
    setViewerOpen(true);

    // ✅ Now scroll the list AFTER the layout change
    requestAnimationFrame(() => {
      ensureActiveCardVisibleInList(id);
    });

    const q = await fetchQuestion(id);
    if (requestSeq !== practiceOpenRequestSeq) return;
    if (!q?.id) throw new Error(q?.error || "Question not found.");

    // ✅ Keep current question in state so Reveal/Explain (wired once in init) can use it
    state.currentQuestion = q;

    els("viewer").hidden = false;
    els("qTitle").textContent = id;

    focusViewer();

    const meta = [];
    if (q.type) meta.push(q.type);
    if (q.paper) meta.push(q.paper);
    if (q.section && q.type !== "objective") meta.push(q.section);
    if (q.marks) meta.push(`${q.marks} marks`);
    if (q.page) meta.push(`page ${q.page}`);

    const tag = [];
    if (q.exam) tag.push(q.exam);
    if (q.year) tag.push(String(q.year));
    if (q.subject) tag.push(q.subject);
    if (tag.length) meta.push(tag.join(" "));

    if (q.diagrams && q.diagrams.length) meta.push(`diagrams: ${q.diagrams.join(", ")}`);

    els("qMeta").textContent = meta.join(" • ");

    // ✅ Render question in the new flow (question-only first)
    renderQuestion(q);
    resetQuestionFeedbackForm();
    setQuestionFeedbackPanelOpen(false);

    // Options
    const optBox = els("qOptions");
    optBox.innerHTML = "";
    if (q.options) {
      for (const k of Object.keys(q.options)) {
        const d = document.createElement("div");
        d.className = "opt";
        d.dataset.key = k;
        d.innerHTML = `<b>${escapeHtml(k)}</b>. ${escapeHtml(q.options[k])}`;

        // visual-only option selection
        d.onclick = () => {
          const alreadySelected = d.classList.contains("selected");

          // clear others first
          optBox.querySelectorAll(".opt").forEach((el) => el.classList.remove("selected"));

          if (alreadySelected) {
            // toggle OFF
            selectedOptionKey = null;
            return;
          }

          // toggle ON
          selectedOptionKey = k;
          d.classList.add("selected");
        };

        optBox.appendChild(d);
      }
    }

    // safe re-sync after render
    updatePrevNextButtons();
  } catch (e) {
    setStatus(`Failed to open question: ${e?.message || e}`, "bad");
  }
}


function closeViewer() {
  practiceOpenRequestSeq += 1;
  state.currentQuestion = null;
  els("viewer").hidden = true;
  setViewerOpen(false);
  clearQuestionHighlight();

  currentIndex = -1;
  updatePrevNextButtons();

  if (els("qPassage")) { els("qPassage").hidden = true; els("qPassage").innerHTML = ""; }
  if (els("qDiagrams")) els("qDiagrams").innerHTML = "";
  els("qOptions").innerHTML = "";
  els("qExplain").hidden = true;
  els("qExplain").innerHTML = "";
  resetQuestionFeedbackForm();
  setQuestionFeedbackPanelOpen(false);
}

window.addEventListener("beforeunload", stopCbtTimer);

function initDiagramLightbox() {
  document.addEventListener("click", handleDiagramLightboxClick);

  els("btnDiagramClose")?.addEventListener("click", () => closeDiagramLightbox());
  els("btnDiagramZoomIn")?.addEventListener("click", () => setDiagramLightboxZoom(diagramLightboxState.scale + DIAGRAM_LIGHTBOX_ZOOM_STEP));
  els("btnDiagramZoomOut")?.addEventListener("click", () => setDiagramLightboxZoom(diagramLightboxState.scale - DIAGRAM_LIGHTBOX_ZOOM_STEP));
  els("btnDiagramZoomReset")?.addEventListener("click", () => resetDiagramLightboxZoom());
  els("diagramLightbox")?.addEventListener("click", (event) => {
    if (event.target === els("diagramLightbox") || event.target === els("diagramLightboxBackdrop") || event.target === els("diagramLightboxStage")) {
      closeDiagramLightbox();
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && diagramLightboxState.isOpen) {
      event.preventDefault();
      closeDiagramLightbox();
    }
  });
}

async function checkApi() {
  saveApiBase();
  setStatus("Checking API…", "ok");
  const r = await api("/health");
  if (r?.ok) setStatus(`Connected: ${r.service}`, "ok");
  else setStatus(`Failed: ${r?.error || "unknown error"}`, "bad");
}

function isValidEmail(s) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(String(s || "").trim());
}

function normalizeEmail(s) {
  return String(s || "").trim().toLowerCase();
}

function syncPayEmailAutofill() {
  const identifierInput = els("identifier");
  const payEmailInput = els("payEmailInput");
  if (!payEmailInput) return;

  const identifierValue = normalizeEmail(identifierInput?.value || state.me?.identifier || "");
  if (isValidEmail(identifierValue)) {
    payEmailInput.value = identifierValue;
    return;
  }

  const storedEmail = normalizeEmail(state.me?.email || "");
  if (isValidEmail(storedEmail) && !payEmailInput.value.trim()) {
    payEmailInput.value = storedEmail;
  }
}

async function resolvePaystackEmail(access) {
  const payEmailInput = els("payEmailInput");
  const inputEmail = normalizeEmail(payEmailInput?.value || "");
  if (payEmailInput && !payEmailInput.hidden && isValidEmail(inputEmail)) {
    return { email: inputEmail, source: "payEmailInput" };
  }

  const profileIdentifier = normalizeEmail(access?.profile?.identifier || "");
  if (isValidEmail(profileIdentifier)) {
    return { email: profileIdentifier, source: "identifier" };
  }

  const formIdentifier = normalizeEmail(els("identifier")?.value || "");
  if (isValidEmail(formIdentifier)) {
    return { email: formIdentifier, source: "identifier" };
  }

  if (state.token) {
    const me = await api("/me");
    if (me?.identifier) {
      applyProfile(me);
      const meEmail = normalizeEmail(me.email || "");
      if (isValidEmail(meEmail)) {
        syncPayEmailAutofill();
        return { email: meEmail, source: "/me" };
      }
    }
  }

  return { email: "", source: "" };
}

function computeAccessUiState(profile) {
  const resolvedProfile = profile || state.me || null;
  const authenticated = !!state.authenticated;
  const isActive = !!state.isPaid;
  const isFounding = !!(resolvedProfile && resolvedProfile.isFounding);
  const plan = (resolvedProfile && resolvedProfile.plan) ? resolvedProfile.plan : "free";
  const isCoreActive = isActive && plan === "core";
  const foundingOpen =
    (state.foundingStatus && typeof state.foundingStatus.open === "boolean")
      ? state.foundingStatus.open
      : true;
  const allowFoundingButton = authenticated && (foundingOpen || isFounding);
  const canRenewFounding = isFounding;
  const showUpgradeHint = !isActive && authenticated && !!state.hasLoadedQuestions;
  const showRefresh =
    authenticated &&
    !isActive &&
    (state.paywalled || !!state.justPaidAttempt);

  let payMessage = "";
  if (!authenticated) payMessage = "Login to upgrade.";
  else if (!foundingOpen && !isFounding) payMessage = "Founding is closed. Please use Core.";
  else if (isCoreActive) payMessage = "Core is active ✅ No renewal needed now.";
  else if (isActive && canRenewFounding) payMessage = "Founding access is active ✅ You can renew ₦1,000 to extend 30 days.";
  else if (isActive) payMessage = "You are already paid ✅";

  return {
    authenticated,
    profile: resolvedProfile,
    isActive,
    isFounding,
    plan,
    isCoreActive,
    foundingOpen,
    allowFoundingButton,
    canRenewFounding,
    showUpgradeHint,
    showRefresh,
    payMessage,
  };
}

function updatePayEmailUI() {
  const label = els("payEmailLabel");   // optional
  const input = els("payEmailInput");   // required
  const hint  = els("payEmailHint");    // optional
  const access = computeAccessUiState();

  // If the input isn't in HTML, we can't show anything
  if (!input) return;

  // Not logged in → never show
  if (!access.authenticated || !access.profile) {
    if (label) label.hidden = true;
    input.hidden = true;
    if (hint) hint.hidden = true;
    input.value = "";
    return;
  }

  const identifier = (access.profile.identifier || "").trim();
  const storedEmail = (access.profile.email || "").trim();

  const needsEmail =
    !isValidEmail(identifier) &&
    !isValidEmail(storedEmail);

  if (label) label.hidden = !needsEmail;
  input.hidden = !needsEmail;
  if (hint) hint.hidden = !needsEmail;

  if (needsEmail) {
    if (!input.value.trim()) syncPayEmailAutofill();
  } else {
    syncPayEmailAutofill();
  }
}

function setTopNavActive(view = "dashboard") {
  const dashboardBtn = els("btnNavDashboard");
  const adminBtn = els("btnNavAdmin");
  if (dashboardBtn) dashboardBtn.classList.toggle("active-nav", view === "dashboard");
  if (adminBtn) adminBtn.classList.toggle("active-nav", view === "admin");
}

function showDashboardView() {
  state.adminView = "dashboard";
  const dashboard = els("dashboardSection");
  const admin = els("adminSection");
  if (dashboard) dashboard.hidden = !state.authenticated;
  if (admin) admin.hidden = true;
  setTopNavActive("dashboard");
}

function showAdminView() {
  if (!state.isAdmin) return;
  state.adminView = "admin";
  const dashboard = els("dashboardSection");
  const admin = els("adminSection");
  if (dashboard) dashboard.hidden = true;
  if (admin) admin.hidden = false;
  setTopNavActive("admin");
  if (admin) admin.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderAdminQuestions(items = [], total = 0) {
  state.adminQuestions = Array.isArray(items) ? items : [];
  currentListIds = state.adminQuestions.map((item) => item.id).filter(Boolean);
  syncCurrentIndexFromId(activeQuestionId || "");
  updatePrevNextButtons();
  const list = els("adminQuestionsList");
  const meta = els("adminQuestionsMeta");
  if (!list || !meta) return;

  meta.textContent = total
    ? `Showing ${state.adminQuestions.length} of ${total} question(s). Admin access bypasses preview limits.`
    : "No questions matched the current filters.";
  list.innerHTML = "";

  if (!state.adminQuestions.length) {
    list.innerHTML = `<div class="status">No questions found for these filters.</div>`;
    return;
  }

  for (const q of state.adminQuestions) {
    const item = document.createElement("div");
    item.className = "item";
    item.dataset.qid = q.id;
    item.onclick = () => openQuestion(q.id);

    const metaBits = [q.exam, q.year, q.subject, q.type].filter(Boolean);
    item.innerHTML = `
      <div class="card-top">
        <span class="qid">${escapeHtml(q.id || "")}</span>
        ${q.type ? `<span class="pill">${escapeHtml(q.type)}</span>` : ""}
      </div>
      <div class="qtext">${escapeHtml(trimText(cleanPreviewText(q.question_text), 160))}</div>
      <div class="meta">${escapeHtml(metaBits.join(" • "))}</div>
    `;
    list.appendChild(item);
  }
}

function renderAdminFeedback(items = [], total = 0) {
  state.adminFeedback = Array.isArray(items) ? items : [];
  const list = els("adminFeedbackList");
  const meta = els("adminFeedbackMeta");
  if (!list || !meta) return;

  meta.textContent = total
    ? `Showing ${state.adminFeedback.length} of ${total} feedback record(s).`
    : "No feedback matched the current filters.";
  list.innerHTML = "";

  if (!state.adminFeedback.length) {
    list.innerHTML = `<div class="status">No feedback records found.</div>`;
    return;
  }

  for (const entry of state.adminFeedback) {
    const item = document.createElement("div");
    item.className = "item admin-feedback-item";

    const questionLink = entry.question_id
      ? `<button class="btn ghost tiny admin-question-link" type="button" data-question-id="${escapeHtml(String(entry.question_id))}">${escapeHtml(String(entry.question_id))}</button>`
      : "—";

    item.innerHTML = `
      <div class="admin-feedback-grid">
        <div><strong>Type:</strong> ${escapeHtml(String(entry.feedback_type || "—"))}</div>
        <div><strong>Question:</strong> ${questionLink}</div>
        <div><strong>Category:</strong> ${escapeHtml(String(entry.category || "—"))}</div>
        <div><strong>Message:</strong> ${escapeHtml(String(entry.message || "—"))}</div>
        <div><strong>Source area:</strong> ${escapeHtml(String(entry.source_area || "—"))}</div>
        <div><strong>User:</strong> ${escapeHtml(String(entry.user_identifier || "anonymous"))}</div>
        <div><strong>Created:</strong> ${escapeHtml(String(entry.created_at || "—"))}</div>
      </div>
    `;

    const questionBtn = item.querySelector("[data-question-id]");
    if (questionBtn) {
      questionBtn.onclick = (event) => {
        event.stopPropagation();
        openQuestion(entry.question_id);
      };
    }

    list.appendChild(item);
  }
}

async function loadAdminQuestions() {
  if (!state.isAdmin) return;
  const result = await fetchAdminQuestions({
    exam: els("adminExamFilter")?.value || "",
    year: els("adminYearFilter")?.value || "",
    subject: els("adminSubjectFilter")?.value || "",
    qtype: els("adminQtypeFilter")?.value || "",
  });

  if (!result || result.ok === false) {
    const meta = els("adminQuestionsMeta");
    if (meta) meta.textContent = result?.error || "Unable to load admin questions.";
    return;
  }

  renderAdminQuestions(result.items || [], Number(result.total || 0));
}

async function loadAdminFeedback() {
  if (!state.isAdmin) return;
  const result = await fetchAdminFeedback({
    feedbackType: els("adminFeedbackTypeFilter")?.value || "",
    sourceArea: els("adminFeedbackSourceFilter")?.value || "",
  });

  if (!result || result.ok === false) {
    const meta = els("adminFeedbackMeta");
    if (meta) meta.textContent = result?.error || "Unable to load feedback records.";
    return;
  }

  renderAdminFeedback(result.items || [], Number(result.total || 0));

  const currentSource = els("adminFeedbackSourceFilter")?.value || "";
  const sourceOptions = [...new Set((result.items || []).map((item) => String(item.source_area || "").trim()).filter(Boolean))].sort();
  populateSelectOptions(els("adminFeedbackSourceFilter"), sourceOptions, "All");
  const sourceSel = els("adminFeedbackSourceFilter");
  if (sourceSel) sourceSel.value = sourceOptions.includes(currentSource) ? currentSource : "";
}

async function setupAdminFilters() {
  const filters = await fetchFilters();
  if (filters && filters.ok !== false) {
    populateSelectOptions(els("adminExamFilter"), filters.exams || [], "All");
    populateSelectOptions(els("adminYearFilter"), (filters.years || []).map(String), "All");
    populateSelectOptions(els("adminSubjectFilter"), filters.subjects || [], "All");
  }
}

function resetSessionProfileState() {
  state.authenticated = false;
  state.isPaid = false;
  state.isAdmin = false;
  state.justPaidAttempt = false;
  state.me = null;

  setPaidChip(false);
  showDashboardView();
  setDashboardMsg("");

  const btnLogout = els("btnLogout");
  if (btnLogout) btnLogout.hidden = true;

  const btnHist = els("btnToggleHistory");
  if (btnHist) btnHist.hidden = true;

  const phBox = els("paymentHistory");
  if (phBox) phBox.hidden = true;

  state.historyOpen = false;
  state.historyLoadedOnce = false;

  const pw = els("paywall");
  if (pw) {
    pw.removeAttribute("hidden");
    pw.classList.remove("is-open");
  }

  updatePayEmailUI();
}

function applyProfile(profile) {
  state.authenticated = true;
  state.me = {
    identifier: String(profile.identifier || "").trim(),
    email: String(profile.email || "").trim(),
    isPaid: !!profile.is_paid,
    isPaidActive: (profile.is_paid_active !== undefined) ? !!profile.is_paid_active : !!profile.is_paid,
    plan: String(profile.plan || "free"),
    isFounding: !!profile.is_founding,
    isAdmin: !!profile.is_admin,
    paidUntil: profile.paid_until ? String(profile.paid_until) : "",
  };

  const nowPaid = !!state.me.isPaidActive;
  state.isPaid = nowPaid;
  state.isAdmin = !!state.me.isAdmin;
  if (state.isPaid) state.justPaidAttempt = false;

  const btnLogout = els("btnLogout");
  if (btnLogout) btnLogout.hidden = false;

  setAuthMsg(`Logged in as: ${state.me.identifier}`);
  setDashboardMsg("Dashboard ready. JAMB CBT will appear here when it ships.");
  updatePayEmailUI();

  const btnHist = els("btnToggleHistory");
  if (btnHist) {
    btnHist.hidden = false;
    btnHist.textContent = state.historyOpen
      ? "Hide payment history"
      : "View payment history";
  }

  const phBox = els("paymentHistory");
  if (phBox) phBox.hidden = !state.historyOpen;

  if (state.historyOpen) loadPaymentHistory().catch(() => {});

  resetIdleTimer();

  if (state.isAdmin && state.adminView === "admin") showAdminView();
  else showDashboardView();

  const activeSection = els(state.isAdmin && state.adminView === "admin" ? "adminSection" : "dashboardSection");
  if (activeSection) {
    activeSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  return { nowPaid };
}

async function loadProfile() {
  state.token = sessionStorage.getItem("token") || "";

  if (!state.token) {
    resetSessionProfileState();
    updateUpgradeUI();
    updateAdminUI();
    return null;
  }

  const wasPaid = !!state.isPaid;
  const r = await api("/me");

  if (r?.identifier) {
    const { nowPaid } = applyProfile(r);

    if (!wasPaid && nowPaid) {
      state.paywalled = false;
      state.endReached = false;
      state.pageIndex = 0;

      const pw = els("paywall");
      if (pw) {
        pw.removeAttribute("hidden");
        pw.classList.remove("is-open");
      }

      loadList(0);
    }
  } else {
    resetSessionProfileState();
  }

  await refreshFoundingStatus();
  updateUpgradeUI();
  updatePlanMetaUI();
  updateAdminUI();
  return state.me || null;
}

async function register(identifier, password) {
  saveApiBase();
  const r = await api("/auth/register", {
    method: "POST",
    body: JSON.stringify({ identifier, password })
  });

  if (r?.token) {
    saveToken(r.token);
    await loadProfile();
  }

  return r;
}

async function login(identifier, password) {
  saveApiBase();
  const r = await api("/auth/login", {
    method: "POST",
    body: JSON.stringify({ identifier, password })
  });

  if (r?.token) {
    saveToken(r.token);
    await loadProfile();
  }

  return r;
}

async function logout() {
  stopIdleTimer();
  saveToken("");

  state.authenticated = false;
  state.isPaid = false;
  state.paywalled = false;
  state.endReached = false;
  state.pageIndex = 0;
  state.hasLoadedQuestions = false;

  resetSessionProfileState();
  setAuthMsg("Logged out.");
  updateDashboardUI();

  const list = els("list");
  if (list) list.innerHTML = "";
  closeViewer?.();

  adminClearKey();
  updateUpgradeUI();
  updateAdminUI();
}

async function loadPaymentHistory() {
  const listEl = els("paymentHistoryList");
  if (!listEl) return;

  if (!state.token) {
    listEl.textContent = "Login to view payment history.";
    return;
  }

  const r = await api("/payments/history?limit=20");
  if (!r || r.ok === false) {
    listEl.textContent = "Unable to load payment history.";
    return;
  }

  const items = Array.isArray(r.items) ? r.items : [];
  if (!items.length) {
    listEl.textContent = "No payments yet.";
    return;
  }

  listEl.innerHTML = items
    .map((p) => {
      const dt = escapeHtml(String(p.created_at || ""));
      const ref = escapeHtml(String(p.reference || ""));
      const amt = escapeHtml(String(p.amount || ""));
      const cur = escapeHtml(String(p.currency || "NGN"));
      const st = escapeHtml(String(p.status || ""));
      const prov = escapeHtml(String(p.provider || "paystack"));
      return `<div style="padding:8px 0;border-bottom:1px solid #eee;">
        <div><b>${amt} ${cur}</b> • ${st}</div>
        <div class="mono small">${prov} • ${ref}</div>
        <div class="small">${dt}</div>
      </div>`;
    })
    .join("");
}

async function doRegister() {
  const identifier = els("identifier").value.trim();
  const password = els("password").value;

  setAuthMsg("Registering…");
  const r = await register(identifier, password);

  if (r?.token) {
    setAuthMsg("Registered ✅");
  } else {
    setAuthMsg(`Register failed: ${r?.error || "unknown error"}`);
  }
}

async function doLogin() {
  const identifier = els("identifier").value.trim();
  const password = els("password").value;

  setAuthMsg("Logging in…");
  const r = await login(identifier, password);

  if (r?.token) {
    setAuthMsg("Logged in ✅");
  } else {
    setAuthMsg(`Login failed: ${r?.error || "unknown error"}`);
  }
}

 async function doLogout() {
  await logout();
}


async function loadList(targetPageIndex = state.pageIndex) {
  saveApiBase();
  state.hasLoadedQuestions = true; // ✅ STEP 2: user attempted to load questions
  updateUpgradeUI();


  const mode = els("mode").value;
  const limit = state.pageSize || 20;
  const pageIndex = Math.max(0, parseInt(targetPageIndex || 0, 10) || 0);
  const offset = pageIndex * limit;
  const requestParams = {
    mode,
    pageIndex,
    offset,
    exam: state.filters.exam,
    year: state.filters.year,
    subject: state.filters.subject,
  };

  if (DEBUG_QUESTIONS) {
    console.debug("[questions] loadList request params", requestParams);
  }

  // keep current list visible unless successful load
  const pw = els("paywall");
 if (pw) pw.classList.remove("is-open");

  setStatus("Loading…", "ok");
  state.paywalled = false;
  setListPagerUI({ loading: true });

  const r = await fetchQuestionPage({
    mode: requestParams.mode,
    limit,
    offset: requestParams.offset,
    exam: requestParams.exam,
    year: requestParams.year,
    subject: requestParams.subject,
  });

  if (DEBUG_QUESTIONS) {
    const returnedItems = Array.isArray(r?.items) ? r.items.length : 0;
    const errorPayload = r?.error ?? r?.payload ?? r?.detail ?? null;
    console.debug("[questions] loadList response", {
      status: r?.status ?? null,
      returnedItems,
      errorPayload,
      ok: !!r?.ok,
      paywall: !!r?.paywall,
    });
  }

  if (!r?.ok && r?.status !== 402 && !r?.paywall) {
    setStatus(`Failed to load questions: ${r?.error || "unknown error"}`, "bad");
    setListPagerUI({ loading: false });
    return;
  }

  // Paywall: show ONLY after user has attempted to load questions
 if (
  state.hasLoadedQuestions &&
  ((r?.ok === false && r?.status === 402) || r?.paywall)
) {
  state.paywalled = true;

  setStatus("Preview limit reached. Please upgrade.", "bad");

  // ✅ SHOW paywall
  const pw = els("paywall");
  pw.removeAttribute("hidden");
  pw.classList.add("is-open");


  // ✅ HIDE passive upgrade hint (no double messaging)
  const upgradeHint = els("upgradeHint");
  if (upgradeHint) upgradeHint.hidden = true;

  setListPagerUI({ loading: false });
  return;
}


  const items = r.items || [];

  // end-of-list: don't show an empty page
  if (!items.length && pageIndex > 0) {
    state.endReached = true;
    setStatus("End reached. No more questions.", "ok");
    setListPagerUI({ loading: false });
    return;
  }

  // success
  localStorage.setItem("ep_started", "1");
  state.pageIndex = pageIndex;
  state.lastItems = items;
 // ✅ Only use endReached heuristic for PAID users.
// For unpaid users, backend may clamp results (preview cap), but that doesn't mean "end".
  state.endReached = !!state.isPaid && (items.length < limit);

  if (DEBUG_QUESTIONS) {
    console.debug("[questions] loadList success items.length", items.length);
    console.debug("[questions] loadList success first question id", items[0]?.id ?? null);
    console.debug("[questions] loadList about to call renderList(items)");
  }

  renderList(items);
  setStatus(`Loaded ${items.length || 0} items.`, "ok");
  if (state.endReached) setStatus("End reached. No more questions.", "ok");

  setListPagerUI({ loading: false });
}
function fmtDate(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  } catch {
    return "";
  }
}

function daysLeft(iso) {
  try {
    const ms = new Date(iso).getTime() - Date.now();
    return Math.ceil(ms / (1000 * 60 * 60 * 24));
  } catch {
    return null;
  }
}

function formatPlanLabel(plan) {
  const normalized = String(plan || "free").trim().toLowerCase();
  if (normalized === "founding") return "Founding";
  if (normalized === "core") return "Core";
  return "Free";
}

function getActiveUntilText(profile = state.me) {
  const paidUntil = profile?.paidUntil || "";
  if (!!state.isPaid && paidUntil) {
    const formatted = fmtDate(paidUntil);
    const dleft = daysLeft(paidUntil);
    const dtext = (dleft !== null && dleft >= 0) ? ` (${dleft} day${dleft === 1 ? "" : "s"} left)` : "";
    return formatted ? `Active until ${formatted}${dtext}` : `Active until ${paidUntil}`;
  }
  return "Free preview";
}

function updateDashboardUI() {
  const section = els("dashboardSection");
  const adminSection = els("adminSection");
  const identifierEl = els("dashboardIdentifier");
  const planEl = els("dashboardPlan");
  const untilEl = els("dashboardPaidUntil");
  const logoutBtn = els("btnDashboardLogout");
  const cbtBtn = els("btnDashboardStartCbt");
  const btnNavDashboard = els("btnNavDashboard");
  const btnNavAdmin = els("btnNavAdmin");

  if (!section || !identifierEl || !planEl || !untilEl) return;

  const isLoggedIn = !!state.authenticated && !!state.me;
  section.hidden = !isLoggedIn;

  if (logoutBtn) logoutBtn.hidden = !isLoggedIn;
  if (cbtBtn) cbtBtn.hidden = !isLoggedIn;
  if (btnNavDashboard) btnNavDashboard.hidden = !isLoggedIn;
  if (btnNavAdmin) btnNavAdmin.hidden = !(isLoggedIn && state.isAdmin);

  if (!isLoggedIn) {
    identifierEl.textContent = "—";
    planEl.textContent = "Free";
    untilEl.textContent = "Free preview";
    if (adminSection) adminSection.hidden = true;
    setTopNavActive("dashboard");
    return;
  }

  identifierEl.textContent = state.me.identifier || state.me.email || "—";
  planEl.textContent = state.isAdmin ? `${formatPlanLabel(state.me.plan)} • Admin` : formatPlanLabel(state.me.plan);
  untilEl.textContent = state.isAdmin ? "Admin access • unrestricted questions and feedback" : getActiveUntilText(state.me);
  if (adminSection) adminSection.hidden = !(state.isAdmin && state.adminView === "admin");
  setTopNavActive(state.isAdmin && state.adminView === "admin" ? "admin" : "dashboard");
}

function updatePlanMetaUI() {
  const box = els("planMeta");
  const badge = els("foundingBadge");
  const until = els("activeUntil");
  if (!box || !badge || !until) return;

  if (!state.authenticated) {
    box.hidden = true;
    updateDashboardUI();
    return;
  }

  const isFounding = !!(state.me && state.me.isFounding);

  box.hidden = false;
  badge.hidden = !isFounding;
  until.textContent = getActiveUntilText(state.me) === "Free preview" ? "" : getActiveUntilText(state.me);
  updateDashboardUI();
}

 function updateUpgradeUI() {
  const btnPay = els("btnPay");
  const btnPayCore = els("btnPayCore");
  const btnCheckPaid = els("btnCheckPaid");
  if (!btnPay || !btnCheckPaid) return;

  const foundingOffer = els("foundingOffer");
  const access = computeAccessUiState();

  // Show/hide ₦1,000 button
  btnPay.hidden = !access.allowFoundingButton;

  // Hide/show the Founding offer copy together with the ₦1,000 button
  if (foundingOffer) foundingOffer.hidden = btnPay.hidden;

  // ✅ Core button visibility rule
  // Show only when logged in AND Core not already active
  if (btnPayCore) {
    btnPayCore.hidden = !access.authenticated || access.isCoreActive;
  }

  // ✅ Upgrade hint: show only AFTER browsing starts, and only for unpaid logged-in users
  const upgradeHint = els("upgradeHint");
  if (upgradeHint) {
    upgradeHint.hidden = !access.showUpgradeHint;
  }

  // ✅ Busy-pay lock (while popup is opening / active)
  if (state.busyPay) {
    btnPay.disabled = true;
    if (btnPayCore) btnPayCore.disabled = true;
    btnCheckPaid.disabled = true;
    return;
  }

  // Disable Pay ₦1,000 when:
  // - not logged in, OR
  // - user is active Core, OR
  // - user is active but NOT a founder (optional)
  btnPay.disabled = !access.authenticated || access.isCoreActive || (access.isActive && !access.canRenewFounding);

  // ✅ IMPORTANT FIX:
  // Re-enable Core button after busyPay ends (unless Core is already active / not logged in)
  if (btnPayCore) {
    btnPayCore.disabled = !access.authenticated || access.isCoreActive;
  }

  btnCheckPaid.hidden = !access.showRefresh;
  btnCheckPaid.disabled = !access.authenticated;

  setPayMsg(access.payMessage);
}



async function refreshFoundingStatus() {
  try {
    const r = await api("/founding/status");
    if (r && typeof r.open === "boolean") state.foundingStatus = r;
  } catch (_) {
    // fail-open so you don't accidentally hide Founding if the endpoint blips
    state.foundingStatus = { open: true };
  }
}


async function getPaystackPublicKeyOrThrow() {
  const r = await api("/payments/public-key", { method: "GET" });
  if (!r?.ok) throw new Error(r?.error || "Failed to get Paystack public key");
  if (!r.public_key || typeof r.public_key !== "string" || !r.public_key.startsWith("pk_")) {
    throw new Error("Backend returned an invalid Paystack public key");
  }
  return r.public_key;
}

function setPayBusy(isBusy, msg) {
  state.busyPay = !!isBusy;
  if (typeof msg === "string") setPayMsg(msg);
  updateUpgradeUI();
}

function getPaymentRecoveryMessage(prefix) {
  const lead = prefix ? `${prefix} ` : "";
  return `${lead}Retry with Card, Bank Transfer, USSD, or OPay via Pay with Bank in the Paystack popup.`;
}

function resetPaymentRetryState(message, { statusMessage = "", statusKind = "", preserveRefresh = true } = {}) {
  setPayBusy(false, message);
  if (statusMessage) setStatus(statusMessage, statusKind || "bad");
  if (preserveRefresh) state.justPaidAttempt = true;
}

async function verifyPayment(reference, email) {
  return await api("/payments/verify", {
    method: "POST",
    body: JSON.stringify({ reference, email }),
  });
}

async function startPayment(amountNgn = PAYSTACK_AMOUNT_NGN) {
  updatePayEmailUI();

  const access = computeAccessUiState();
  if (!access.authenticated || !access.profile) {
    setStatus("Please login before paying.", "bad");
    setPayMsg("Login to upgrade.");
    return;
  }

  // ✅ mark that user has attempted payment (helps show Refresh Paid Status when useful)
  state.justPaidAttempt = true;
  updateUpgradeUI();

  if (!window.PaystackPop || typeof window.PaystackPop.setup !== "function") {
    setStatus("Paystack script not loaded. Please reload the page.", "bad");
    setPayMsg("Paystack failed to load. Check your connection and reload.");
    return;
  }

  const identifier = normalizeEmail(access.profile.identifier || els("identifier")?.value || "");
  const payEmailInput = els("payEmailInput");
  syncPayEmailAutofill();

  const { email: payEmail, source } = await resolvePaystackEmail(access);
  if (!isValidEmail(payEmail)) {
    updatePayEmailUI();
    if (payEmailInput) {
      payEmailInput.hidden = false;
      payEmailInput.focus();
    }
    setStatus("Please enter a valid receipt email to continue.", "bad");
    setPayMsg("A valid email is required before checkout can open.");
    return;
  }

  if (source === "payEmailInput" && payEmail !== normalizeEmail(state.me?.email || "")) {
    const up = await api("/me/email", {
      method: "POST",
      body: JSON.stringify({ email: payEmail }),
    });

    if (!up?.ok) {
      setStatus(`Could not save email: ${up?.error || "unknown"}`, "bad");
      setPayMsg("Please try again.");
      return;
    }

    if (state.me) state.me.email = payEmail;
    updatePayEmailUI();
  }

  setPayBusy(true, "Opening Paystack…");

  try {
    const pk = await getPaystackPublicKeyOrThrow();
    if (!pk) throw new Error("Could not load Paystack public key");

    const amount = Number(amountNgn || 0) * 100; // kobo

    const handler = PaystackPop.setup({
      key: pk,
      email: payEmail,
      amount,
      currency: "NGN",
      metadata: {
        custom_fields: [
          {
            display_name: "ExamPartner Identifier",
            variable_name: "identifier",
            value: identifier,
          },
        ],
      },

      callback: function (resp) {
        (async () => {
          const reference = resp?.reference;
          if (!reference) {
            resetPaymentRetryState(
              getPaymentRecoveryMessage("We couldn't confirm the payment reference."),
              { statusMessage: "Payment returned no reference. Please try again.", statusKind: "bad" }
            );
            return;
          }

          setPayBusy(true, "Verifying payment…");
          const vr = await verifyPayment(reference, payEmail);

          if (!vr?.ok) {
            resetPaymentRetryState(
              `Verification did not complete for ref ${reference}. Retry with Card, Bank Transfer, USSD, or OPay via Pay with Bank in the Paystack popup.`,
              {
                statusMessage: `Payment received but verification failed: ${vr?.error || "unknown"}`,
                statusKind: "bad",
              }
            );
            return;
          }

          await refreshMe(); // refreshMe will clear justPaidAttempt if user is now paid

          setPayBusy(false, "");
          setStatus("Payment verified ✅", "ok");
          setPayMsg(`Paid ✅ Ref: ${reference}`);
          updateUpgradeUI();
        })().catch((e) => {
          resetPaymentRetryState(
            getPaymentRecoveryMessage("We couldn't verify the payment right now."),
            { statusMessage: `Pay verify error: ${e?.message || e}`, statusKind: "bad" }
          );
        });
      },

      onClose: function () {
        resetPaymentRetryState(
          getPaymentRecoveryMessage("Checkout was closed before payment finished."),
          { statusMessage: "Payment cancelled.", statusKind: "bad" }
        );
      },
    });

    handler.openIframe();
  } catch (e) {
    resetPaymentRetryState(
      getPaymentRecoveryMessage("We couldn't start the Paystack checkout."),
      { statusMessage: `Pay error: ${e?.message || e}`, statusKind: "bad" }
    );
  }
}

async function checkPaidStatus() {
  await refreshMe();
  setStatus(state.isPaid ? "Paid ✅" : "Not paid yet.", state.isPaid ? "ok" : "bad");
}

async function startPaystackPayment(amountNgn = PAYSTACK_AMOUNT_NGN) {
  return startPayment(amountNgn);
}

async function refreshMe() {
  return loadProfile();
}

/* =========================
   Admin mini tools (SAFE MVP)
   ========================= */

function adminSetKey() {
  const v = window.prompt("Enter Admin Key (server ADMIN_SECRET):");
  if (!v) return;
  state.adminKey = v.trim();
  sessionStorage.setItem(ADMIN_KEY_STORAGE, state.adminKey);
  updateAdminUI();
  setPayMsg("Admin mode enabled (session only).");
}

function adminClearKey() {
  state.adminKey = "";
  sessionStorage.removeItem(ADMIN_KEY_STORAGE);
  updateAdminUI();
  const box = els("auditBox");
  if (box) {
    box.textContent = "";
    box.hidden = true;
  }
  setPayMsg("Admin mode exited.");
}

function updateAdminUI() {
  const tools = els("adminTools");
  if (tools) tools.hidden = !(state.devMode && state.adminKey);

  const btnAdmin = els("btnAdmin");
  if (btnAdmin) btnAdmin.hidden = !state.devMode;
}

async function adminReconcile() {
  if (!state.adminKey) return setStatus("Admin key not set.", "bad");

  const ref = (els("adminRef")?.value || "").trim();
  if (!ref) return setStatus("Enter a reference to reconcile.", "bad");

  setStatus("Reconciling…", "ok");

  const r = await api(`/admin/reconcile/${encodeURIComponent(ref)}`, {
    method: "POST",
    headers: { "x-admin-key": state.adminKey },
  });

  if (!r?.ok) return setStatus(`Reconcile failed: ${r?.error || "unknown"}`, "bad");

  setStatus(`Reconciled: paid=${!!r.paid}`, r.paid ? "ok" : "bad");
  setPayMsg(`Admin reconcile done. Ref: ${ref}`);
  await refreshMe().catch(() => {});
}

async function adminRefund() {
  if (!state.adminKey) return setStatus("Admin key not set.", "bad");

  const ref = (els("adminRef")?.value || "").trim();
  if (!ref) return setStatus("Enter a reference to refund.", "bad");

  const amountStr = (els("refundAmount")?.value || "").trim();
  const note = (els("refundNote")?.value || "").trim();

  const payload = {
    reference: ref,
    amount_kobo: amountStr ? Number(amountStr) : null,
    merchant_note: note || null,
    customer_note: null,
  };

  // clean nulls (backend accepts omit or null, but let's be neat)
  if (!payload.amount_kobo) delete payload.amount_kobo;
  if (!payload.merchant_note) delete payload.merchant_note;
  delete payload.customer_note;

  const ok = window.confirm(
    `Refund transaction?\n\nReference: ${ref}\nAmount(kobo): ${amountStr || "FULL"}\n\nProceed?`
  );
  if (!ok) return;

  setStatus("Sending refund…", "ok");

  const r = await api(`/admin/refund`, {
    method: "POST",
    headers: { "x-admin-key": state.adminKey },
    body: JSON.stringify(payload),
  });

  if (!r?.ok) return setStatus(`Refund failed: ${r?.error || "unknown"}`, "bad");

  setStatus("Refund requested ✅ (webhook will confirm)", "ok");
  setPayMsg(`Refund queued. Ref: ${ref}`);
}

function formatAudit(items) {
  if (!items || !items.length) return "No audit logs found.";
  const lines = [];
  for (const x of items) {
    lines.push(
      [
        `#${x.id}  ${x.created_at}`,
        `action: ${x.action}`,
        x.reference ? `ref: ${x.reference}` : null,
        x.actor_ip ? `ip: ${x.actor_ip}` : null,
        x.user_agent ? `ua: ${x.user_agent}` : null,
        x.payload_json ? `payload: ${x.payload_json}` : null,
        "----",
      ].filter(Boolean).join("\n")
    );
  }
  return lines.join("\n");
}

async function adminFetchAudit() {
  if (!state.adminKey) return setStatus("Admin key not set.", "bad");

  const limit = Math.max(1, Math.min(200, Number((els("auditLimit")?.value || "20")) || 20));
  setStatus("Fetching audit logs…", "ok");

  // ✅ Backend endpoint in your code is /admin/audit
  const r = await api(`/admin/audit?limit=${encodeURIComponent(String(limit))}`, {
    method: "GET",
    headers: { "x-admin-key": state.adminKey },
  });

  if (!r?.ok) return setStatus(`Audit fetch failed: ${r?.error || "unknown"}`, "bad");

  const box = els("auditBox");
  if (box) {
    box.textContent = formatAudit(r.items || []);
    box.hidden = false;
  }

  setStatus(`Loaded ${r.items?.length || 0} audit logs.`, "ok");
}


function adminClearAuditBox() {
  const box = els("auditBox");
  if (!box) return;
  box.textContent = "";
  box.hidden = true;
}

function setupPaymentHistoryToggle() {
  const btn = els("btnToggleHistory");
  const box = els("paymentHistory");
  if (!btn || !box) return;

  btn.onclick = async () => {
    state.historyOpen = !state.historyOpen;

    box.hidden = !state.historyOpen;
    btn.textContent = state.historyOpen
      ? "Hide payment history"
      : "View payment history";

    // Lazy-load once
    if (state.historyOpen && !state.historyLoadedOnce) {
      state.historyLoadedOnce = true;
      await loadPaymentHistory().catch(() => {});
    }
  };
}


// ====== Init ======
async function init() {
  els("yr").textContent = new Date().getFullYear();
  els("apiBase").value = state.apiBase;

  // ✅ used by Reveal/Explain handlers (wired once)
  state.currentQuestion = null;
  state.lastFeedbackSubmission = null;

  // Dev mode: only when URL has ?dev=1 (so normal local testing can still be "user mode")
  const devMode = isDev;
  state.devMode = devMode;
  setPaidChip(false);

  // Status: dev-only (user mode stays clean)
  const statusEl = els("status");
  if (statusEl) statusEl.hidden = !state.devMode;

  // Check API button: dev-only
  const btnCheck = els("btnCheck");
  if (btnCheck) btnCheck.hidden = !state.devMode;

  // In user mode, force the hosted backend and hide all dev/admin tools
  if (!devMode) {
    state.apiBase = "https://exampartner-backend.onrender.com";
    localStorage.removeItem("apiBase");

    const devServerCol = els("devServerCol");
    const devActionsCol = els("devActionsCol");
    if (devServerCol) devServerCol.hidden = true;
    if (devActionsCol) devActionsCol.hidden = true;

    // extra safety: keep status + check hidden even if layout changes
    if (statusEl) statusEl.hidden = true;
    if (btnCheck) btnCheck.hidden = true;
  } else {
    // In dev mode, show server tools so you can point to local backend
    const devServerCol = els("devServerCol");
    const devActionsCol = els("devActionsCol");
    if (devServerCol) devServerCol.hidden = false;
    if (devActionsCol) devActionsCol.hidden = false;

    if (statusEl) statusEl.hidden = false;
    if (btnCheck) btnCheck.hidden = false;
  }

  // Reflect final chosen backend in the input
  const apiBaseEl = els("apiBase");
  if (apiBaseEl) apiBaseEl.value = state.apiBase;

  await initFiltersUI();
  await setupAdminFilters();

  const modeEl = els("mode");
  if (modeEl) {
    modeEl.onchange = () => {
      if (!filtersReady()) { setStartGateVisible(true); return; }
      state.pageIndex = 0;
      state.endReached = false;
      state.paywalled = false;
      loadList(0);
    };
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

  setupPaymentHistoryToggle();
  setupSupportUi();

  updatePracticeMetaUI();
  updateCbtSetupMeta();
  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateAdminUI();
  setListPagerUI({ loading: false });

  // First-time gate vs returning user auto-load
  if (isFirstTimeUser() && !filtersReady()) {
    setStartGateVisible(true);
  } else {
    setStartGateVisible(false);
    if (filtersReady()) {
      state.pageIndex = 0;
      state.endReached = false;
      state.paywalled = false;
      loadList(0);
    }
  }

  // ✅ Safe event wiring (no null-crash)
  if (btnCheck) btnCheck.onclick = checkApi;

  const btnRegister = els("btnRegister");
  if (btnRegister) btnRegister.onclick = doRegister;

  const btnLogin = els("btnLogin");
  if (btnLogin) btnLogin.onclick = doLogin;

  const identifierInput = els("identifier");
  if (identifierInput) {
    identifierInput.addEventListener("input", () => {
      syncPayEmailAutofill();
      updatePayEmailUI();
    });
  }

  const payEmailInput = els("payEmailInput");
  if (payEmailInput) {
    payEmailInput.addEventListener("input", () => {
      if (!payEmailInput.value.trim()) syncPayEmailAutofill();
    });
  }

  const btnLogout = els("btnLogout");
  if (btnLogout) btnLogout.onclick = doLogout;

  const btnNavDashboard = els("btnNavDashboard");
  if (btnNavDashboard) btnNavDashboard.onclick = showDashboardView;

  const btnNavAdmin = els("btnNavAdmin");
  if (btnNavAdmin) {
    btnNavAdmin.onclick = async () => {
      showAdminView();
      if (!state.adminQuestions.length) await loadAdminQuestions();
      if (!state.adminFeedback.length) await loadAdminFeedback();
    };
  }

  const btnDashboardLogout = els("btnDashboardLogout");
  if (btnDashboardLogout) btnDashboardLogout.onclick = doLogout;

  const btnDashboardStartCbt = els("btnDashboardStartCbt");
  if (btnDashboardStartCbt) btnDashboardStartCbt.onclick = () => {
    els("cbtSection")?.removeAttribute("hidden");
    loadCbtSession();
    setDashboardMsg("JAMB CBT session opened below.");
  };

  const btnCbtStartSession = els("btnCbtStartSession");
  if (btnCbtStartSession) btnCbtStartSession.onclick = () => {
    els("cbtSection")?.removeAttribute("hidden");
    loadCbtSession();
  };

  const btnCbtReloadSession = els("btnCbtReloadSession");
  if (btnCbtReloadSession) btnCbtReloadSession.onclick = () => {
    els("cbtSection")?.removeAttribute("hidden");
    loadCbtSession();
  };

  const btnClose = els("btnClose");
  if (btnClose) btnClose.onclick = closeViewer;

  const btnPractice = els("btnPractice");
  if (btnPractice) btnPractice.onclick = () => {
    if (!filtersReady()) {
      setStartGateVisible(true);
      return;
    }
    state.pageIndex = 0;
    state.endReached = false;
    state.paywalled = false;
    loadList(0);

    // bring the list into view on mobile
    const list = els("list");
    if (list) list.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  // List pager (separate from question viewer Prev/Next)
  const btnPrevPage = els("btnPrevPage");
  if (btnPrevPage) btnPrevPage.onclick = () => {
    if (state.pageIndex <= 0) return;
    state.endReached = false;
    state.paywalled = false;
    loadList(state.pageIndex - 1);
  };

  const btnNextPage = els("btnNextPage");
  if (btnNextPage) btnNextPage.onclick = () => {
    if (state.endReached || state.paywalled) return;
    loadList(state.pageIndex + 1);
  };

  const btnCbtPrev = els("btnCbtPrev");
  if (btnCbtPrev) btnCbtPrev.onclick = () => moveCbtQuestion(-1);

  const btnCbtNext = els("btnCbtNext");
  if (btnCbtNext) btnCbtNext.onclick = () => moveCbtQuestion(1);

  const btnCbtSubmit = els("btnCbtSubmit");
  if (btnCbtSubmit) btnCbtSubmit.onclick = () => submitCurrentCbtSession({ reason: "manual" });

  const btnResultBackToCbt = els("btnResultBackToCbt");
  if (btnResultBackToCbt) {
    btnResultBackToCbt.onclick = () => {
      els("cbtSection")?.removeAttribute("hidden");
      els("resultSection")?.setAttribute("hidden", "hidden");
      renderCbtQuestion();
      els("cbtSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  }

  const btnResultStartNew = els("btnResultStartNew");
  if (btnResultStartNew) {
    btnResultStartNew.onclick = async () => {
      els("cbtSection")?.removeAttribute("hidden");
      updateCbtSetupMeta();
      await loadCbtSession();
      els("cbtSection")?.scrollIntoView({ behavior: "smooth", block: "start" });
    };
  }

  const btnPay = els("btnPay");
  if (btnPay) btnPay.onclick = () => startPaystackPayment(PAYSTACK_AMOUNT_NGN);

  const btnPayCore = els("btnPayCore");
  if (btnPayCore) btnPayCore.onclick = () => startPaystackPayment(PAYSTACK_CORE_AMOUNT_NGN);

  const btnCheckPaid = els("btnCheckPaid");
  if (btnCheckPaid) btnCheckPaid.onclick = checkPaidStatus;

  const btnCbtReportQuestion = els("btnCbtReportQuestion");
  if (btnCbtReportQuestion) {
    btnCbtReportQuestion.onclick = () => {
      if (state.cbt.submitted) return;
      const willOpen = els("cbtQuestionFeedbackPanel")?.hidden !== false;
      if (willOpen) setCbtQuestionFeedbackStatus("");
      setCbtQuestionFeedbackPanelOpen(willOpen, { focusMessage: willOpen });
    };
  }

  const btnCancelCbtQuestionFeedback = els("btnCancelCbtQuestionFeedback");
  if (btnCancelCbtQuestionFeedback) {
    btnCancelCbtQuestionFeedback.onclick = () => {
      resetCbtQuestionFeedbackForm();
      setCbtQuestionFeedbackPanelOpen(false);
    };
  }

  const cbtQuestionFeedbackForm = els("cbtQuestionFeedbackForm");
  if (cbtQuestionFeedbackForm) cbtQuestionFeedbackForm.onsubmit = handleCbtQuestionFeedbackSubmit;

  const btnReportQuestion = els("btnReportQuestion");
  if (btnReportQuestion) {
    btnReportQuestion.onclick = () => {
      const willOpen = els("questionFeedbackPanel")?.hidden !== false;
      if (willOpen) setQuestionFeedbackStatus("");
      setQuestionFeedbackPanelOpen(willOpen, { focusMessage: willOpen });
    };
  }

  const btnCancelQuestionFeedback = els("btnCancelQuestionFeedback");
  if (btnCancelQuestionFeedback) {
    btnCancelQuestionFeedback.onclick = () => {
      resetQuestionFeedbackForm();
      setQuestionFeedbackPanelOpen(false);
    };
  }

  const questionFeedbackForm = els("questionFeedbackForm");
  if (questionFeedbackForm) questionFeedbackForm.onsubmit = handleQuestionFeedbackSubmit;

  // ✅ D) Wire Reveal/Explain ONCE here (uses state.currentQuestion)
  
const btnReveal = els("btnReveal");
  if (btnReveal) {
    btnReveal.onclick = () => {
      const q = state.currentQuestion;
      if (!q) return;

      const exp = els("qExplain");
      if (!exp) return;

      exp.hidden = false;
      exp.innerHTML = renderAnswerBlock(q);
      scrollToExplainBox();
    };
  }

  const btnExplain = els("btnExplain");

  if (btnExplain) {
    btnExplain.onclick = () => {
      const q = state.currentQuestion;
      if (!q) return;

      const exp = els("qExplain");
      if (!exp) return;

      exp.hidden = false;
      exp.innerHTML = renderExplainBlock(q);
      scrollToExplainBox();
    };
  }

  // Viewer prev/next question buttons
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

  // Admin buttons
  const btnAdmin = els("btnAdmin");
  if (btnAdmin) btnAdmin.onclick = adminSetKey;

  const btnAdminReconcile = els("btnAdminReconcile");
  if (btnAdminReconcile) btnAdminReconcile.onclick = adminReconcile;

  const btnAdminRefund = els("btnAdminRefund");
  if (btnAdminRefund) btnAdminRefund.onclick = adminRefund;

  const btnAdminAudit = els("btnAdminAudit");
  if (btnAdminAudit) btnAdminAudit.onclick = adminFetchAudit;

  const btnAdminAuditClear = els("btnAdminAuditClear");
  if (btnAdminAuditClear) btnAdminAuditClear.onclick = adminClearAuditBox;

  const btnAdminClear = els("btnAdminClear");
  if (btnAdminClear) btnAdminClear.onclick = adminClearKey;

  const btnAdminLoadQuestions = els("btnAdminLoadQuestions");
  if (btnAdminLoadQuestions) btnAdminLoadQuestions.onclick = loadAdminQuestions;

  const btnAdminResetQuestions = els("btnAdminResetQuestions");
  if (btnAdminResetQuestions) {
    btnAdminResetQuestions.onclick = async () => {
      ["adminExamFilter", "adminYearFilter", "adminSubjectFilter", "adminQtypeFilter"].forEach((id) => {
        const el = els(id);
        if (el) el.value = "";
      });
      await loadAdminQuestions();
    };
  }

  const btnAdminLoadFeedback = els("btnAdminLoadFeedback");
  if (btnAdminLoadFeedback) btnAdminLoadFeedback.onclick = loadAdminFeedback;

  const btnAdminResetFeedback = els("btnAdminResetFeedback");
  if (btnAdminResetFeedback) {
    btnAdminResetFeedback.onclick = async () => {
      ["adminFeedbackTypeFilter", "adminFeedbackSourceFilter"].forEach((id) => {
        const el = els(id);
        if (el) el.value = "";
      });
      await loadAdminFeedback();
    };
  }

  setupIdleTimeout();

  // ✅ load founding cap + me before first render of upgrade UI
  await refreshFoundingStatus();
  await refreshMe();
  updateUpgradeUI(); // ensures btnPay hidden reflects cap immediately

}

init().catch((e)=>console.error(e));
