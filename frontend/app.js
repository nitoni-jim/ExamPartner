

// ExamPartner client (auth + study + CBT + Paystack upgrade) + filters + admin tools

const els = (id) => document.getElementById(id);
const apiBaseNoSlash = () => (state.apiBase || "").replace(/\/$/, "");
const FILTERS_PANEL_OPEN = "ep_filters_open";
const FILTER_CACHE_KEY = "ep_filter_cache_v1";
const FILTER_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

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

// v12.2: extract numeric Q-number from theory question IDs (e.g. "WAEC_2020_MATH_Q10" -> 10)
// Falls back to MAX_SAFE_INTEGER so unmatched IDs sort last.
function extractTheoryQNumber(id) {
  const match = String(id || "").match(/_Q(\d+)$/);
  return match ? parseInt(match[1], 10) : Number.MAX_SAFE_INTEGER;
}

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

  // ✅ Add proper classes
  img.className = `diagram-img ${extraClass}`.trim();

  img.src = diagramSrc(name);
  img.dataset.diagramName = name;
  img.dataset.zoomableDiagram = "true";

  // ✅ CRITICAL: control size inline (fallback if CSS not applied)
  img.style.maxWidth = "100%";
  img.style.maxHeight = "220px";
  img.style.objectFit = "contain";
  img.style.display = "block";
  img.style.margin = "10px auto";
  img.style.cursor = "zoom-in";

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
let studyOpenRequestSeq = 0;

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

// Allowed inline HTML tags that may appear in question/passage text.
// Only these exact tags (no attributes) are rendered — everything else is escaped.
const ALLOWED_INLINE_TAGS = ["u", "i", "em", "b", "strong", "sup", "sub"];
const ALLOWED_TAG_RE = new RegExp(
  `<(/?(${ALLOWED_INLINE_TAGS.join("|")}))>`,
  "gi"
);

function renderTextWithDiagrams(rawText, ctx = {}) {
  const raw = String(rawText || "");

  // Step 1: Extract allowed tags using placeholders so escapeHtml can't touch them.
  const extracted = [];
  const withPlaceholders = raw.replace(ALLOWED_TAG_RE, (match) => {
    const idx = extracted.length;
    extracted.push(match.toLowerCase()); // normalise to lowercase
    return `\x00TAG${idx}\x00`;
  });

  // Step 2: Escape everything else.
  const safe = escapeHtml(withPlaceholders);

  // Step 3: Restore allowed tags and convert newlines to <br>.
  const withTagsBack = safe
    .replace(/\x00TAG(\d+)\x00/g, (_, i) => extracted[parseInt(i, 10)])
    .replace(/\n/g, "<br>");

  const question = ctx.question || null;
  const tables = ctx.tables || question?.tables || {};
  const mode = ctx.mode || "question"; // "question" | "reveal" | "explain"

  // 1) Inject TABLE placeholders: [[table:T1]] or [[table:T1:answer]]
  let out = withTagsBack.replace(/\[\[table:([^\]:\]]+)(?::(answer))?\]\]/gi, (_m, key, answerFlag) => {
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
const PAYSTACK_AMOUNT_NGN = 1000;      // ₦1,000 (Founding — limited to 500 students)
const PAYSTACK_CURRENCY = "NGN";
const PAYSTACK_CORE_AMOUNT_NGN = 2000;  // ₦2,000 (Core — 1 year)
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
  freeYear: null,        // oldest available year for the current subject (free access)

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
    freeYear: null,        // oldest year loaded for free users
    timerDurationMs: 0,
    timeRemainingMs: 0,
    timerStartedAt: 0,
    timerIntervalId: null,
    timerExpired: false,
    submitted: false,
    result: null,
    feedbackOpen: false,
    selectedSubjects: [],   // ["Use of English", subj2, subj3, subj4]
    availableSubjects: [],  // full list from /filters, used by syncCbtSubjectSelectors
    currentSubject: null,   // active tab subject name
    subjectMap: {},         // { "Mathematics": [0,1,...39], ... } — built after load
    subjectTimeMap: {},     // { "Mathematics": totalMs, ... } — accumulated time per subject
    subjectEnteredAt: 0,    // timestamp when user entered current subject
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
    source_area: "study",
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
    source_area: String(payload?.source_area || "study").trim() || "study",
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




function updateStudyMetaUI() {
  const el = els("studyMeta");
  if (!el) return;

  if (!state.filters.exam || !state.filters.subject) {
    el.textContent = "Past Questions • Study Mode";
    return;
  }

  el.textContent = `${state.filters.exam} ${state.filters.year || ""} • ${state.filters.subject}`.trim();
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

function stripLeadingQuestionNumber(text) {
  const raw = String(text || "").trim();

  // Removes:
  // 1. Question
  // 12. Question
  // 45) Question
  // 7 - Question
  return raw.replace(/^\s*\d+\s*([.)-])\s*/, "");
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
      const rendered = renderTextWithDiagrams(String(item || ""), { question, mode: "explain" });
      if (item.startsWith("Option")) {
        return `<p style="margin:6px 0;"><strong>${rendered}</strong></p>`;
      } else if (item.startsWith("Memory hook")) {
        return `<p style="margin:6px 0;"><em>${rendered}</em></p>`;
      } else {
        return `<p style="margin:6px 0;">${rendered}</p>`;
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
  const qTextEl = els("qText");
  const qTablesEl = els("qTables");
  const qDiagramsEl = els("qDiagrams");
  const qSubQuestionsEl = els("qSubQuestions");
  const qSectionInstructionEl = els("qSectionInstruction");
  const qExplainEl = els("qExplain");

  // ✅ SECTION INSTRUCTION (FIXED)
  if (qSectionInstructionEl) {
    const instruction = (question.section_instruction || "").trim();

    if (instruction) {
      qSectionInstructionEl.hidden = false;
      qSectionInstructionEl.innerHTML = `
        <div style="
          margin-bottom:10px;
          padding:10px;
          border-left:4px solid #3b82f6;
          background:rgba(59,130,246,0.08);
          font-style:italic;
          line-height:1.5;
        ">
          ${renderTextWithDiagrams(instruction, {
            question,
            tables: question.tables || {},
            mode: "question"
          })}
        </div>
      `;
    } else {
      qSectionInstructionEl.hidden = true;
      qSectionInstructionEl.innerHTML = "";
    }
  }

  // Passage
  const passageHtml = getPassageDisplayHtml(question.passage_snapshot);
  if (qPassageEl) {
    qPassageEl.hidden = !passageHtml;
    qPassageEl.innerHTML = passageHtml;
  }

  // Question text
  const hasInlineTableRef = /\[\[table:[A-Za-z0-9_]+\]\]/.test(question.question_text || "");

  if (qTextEl) {
    qTextEl.innerHTML = `<div>${renderTextWithDiagrams(question.question_text || "", {
      question,
      tables: question.tables || {},
      mode: "question"
    })}</div>`;
  }

  // Tables
  if (qTablesEl) {
    if (hasInlineTableRef) {
      qTablesEl.innerHTML = "";
      qTablesEl.hidden = true;
    } else {
      const hasTables = question.tables && Object.keys(question.tables).length > 0;
      qTablesEl.hidden = !hasTables;

      if (hasTables) {
        renderTablesInto(qTablesEl, question.tables, question.table_refs || null, "question");
      } else {
        qTablesEl.innerHTML = "";
      }
    }
  }

  // Diagrams
  if (qDiagramsEl) {
    const hasDiagrams = Array.isArray(question.diagrams) && question.diagrams.length > 0;
    qDiagramsEl.hidden = !hasDiagrams;

    if (hasDiagrams) {
      renderDiagramsInto(qDiagramsEl, question.diagrams, { variant: "block" });
    } else {
      qDiagramsEl.innerHTML = "";
    }
  }

  // Sub-questions
  if (qSubQuestionsEl) {
    if (Array.isArray(question.sub_questions) && question.sub_questions.length) {
      qSubQuestionsEl.hidden = false;
      qSubQuestionsEl.innerHTML = `
        <div style="font-weight:700; margin:12px 0 6px;">Sub-questions</div>
        ${renderSubQuestions(question, question.sub_questions, {
          showAnswers: false,
          showExplanations: false,
          showDiagrams: true,
          mode: "question"
        })}
      `;
    } else {
      qSubQuestionsEl.hidden = true;
      qSubQuestionsEl.innerHTML = "";
    }
  }

  // Reset explanation
  if (qExplainEl) {
    qExplainEl.hidden = true;
    qExplainEl.innerHTML = "";
  }
}

 function renderQuestionInto(
  prefix,
  question,
  {
    selectedOptionKey = null,
    onOptionSelect = null,
    readOnly = false,
    reviewAnswer = null,
    stripQuestionNumber = false,
    questionNumber = null,   // CBT position number to prepend (1-based)
    stripSectionRange = false, // CBT: strip "Questions N–M are/: " from section instruction
  } = {}
) {
  const sectionInstructionEl = els(`${prefix}SectionInstruction`);
  const passageEl = els(`${prefix}Passage`);
  const textEl = els(`${prefix}QuestionText`);
  const tablesEl = els(`${prefix}QuestionTables`);
  const diagramsEl = els(`${prefix}QuestionDiagrams`);
  const subQuestionsEl = els(`${prefix}SubQuestions`);
  const optionsEl = els(`${prefix}Options`);

  function stripLeadingQuestionNumber(text) {
    const raw = String(text || "").trim();
    return raw.replace(/^\s*\d+\s*([.)-])\s*/, "");
  }

  function stripQuestionRangeFromInstruction(text) {
    // Removes leading "Questions N–M are " / "Questions N–M: " / "Question N is "
    // e.g. "Questions 27–30 are based on..." → "based on..."
    return String(text || "").trim()
      .replace(/^Questions?\s+\d+[\u2013\u2014-]\d+\s*(are|is|:)\s*/i, "")
      .replace(/^Questions?\s+\d+\s*(is|are|:)\s*/i, "")
      .trim();
  }

  // Normalize once
  const userKey = normalizeCbtAnswerKey(selectedOptionKey);
  const correctKey = normalizeCbtAnswerKey(reviewAnswer);

  // Section instruction
  if (sectionInstructionEl) {
    const rawInstruction = question.section_instruction || "";
    const instruction = (stripSectionRange && rawInstruction)
      ? stripQuestionRangeFromInstruction(rawInstruction)
      : rawInstruction;
    sectionInstructionEl.hidden = !instruction;

    sectionInstructionEl.innerHTML = instruction
      ? `<div class="section-instruction">
          ${renderTextWithDiagrams(instruction, {
            question,
            tables: question.tables || {},
            mode: "question"
          })}
        </div>`
      : "";
  }

  // Passage
  const passageHtml = getPassageDisplayHtml(question.passage_snapshot);
  if (passageEl) {
    passageEl.hidden = !passageHtml;
    passageEl.innerHTML = passageHtml;
  }

  // Question text
  const hasInlineTableRef = /\[\[table:[A-Za-z0-9_]+\]\]/.test(question.question_text || "");
  const strippedText = stripQuestionNumber
    ? stripLeadingQuestionNumber(question.question_text || "")
    : (question.question_text || "");

  // Prepend CBT position number if provided
  const questionTextToRender = (questionNumber !== null)
    ? `${questionNumber}. ${strippedText}`
    : strippedText;

  if (textEl) {
    textEl.innerHTML = `<div>${renderTextWithDiagrams(questionTextToRender, {
      question,
      tables: question.tables || {},
      mode: "question"
    })}</div>`;
  }

  // Tables
  if (tablesEl) {
    if (hasInlineTableRef) {
      tablesEl.innerHTML = "";
      tablesEl.hidden = true;
    } else {
      const hasTables = question.tables && Object.keys(question.tables).length > 0;
      tablesEl.hidden = !hasTables;

      if (hasTables) {
        renderTablesInto(tablesEl, question.tables, question.table_refs || null, "question");
      } else {
        tablesEl.innerHTML = "";
      }
    }
  }

  // Diagrams
  if (diagramsEl) {
    const hasDiagrams = Array.isArray(question.diagrams) && question.diagrams.length > 0;
    diagramsEl.hidden = !hasDiagrams;

    if (hasDiagrams) {
      renderDiagramsInto(diagramsEl, question.diagrams, { variant: "block" });
    } else {
      diagramsEl.innerHTML = "";
    }
  }

  // Sub-questions
  if (subQuestionsEl) {
    if (Array.isArray(question.sub_questions) && question.sub_questions.length) {
      subQuestionsEl.hidden = false;
      subQuestionsEl.innerHTML = `
        <div style="font-weight:700; margin:12px 0 6px;">Sub-questions</div>
        ${renderSubQuestions(question, question.sub_questions, {
          showAnswers: false,
          showExplanations: false,
          showDiagrams: true,
          mode: "question"
        })}
      `;
    } else {
      subQuestionsEl.hidden = true;
      subQuestionsEl.innerHTML = "";
    }
  }

  // Options
  if (optionsEl) {
    optionsEl.innerHTML = "";

    // Review status
    if (reviewAnswer !== null) {
      let statusClass = "unanswered";
      let statusText = "— Not answered";

      if (userKey && userKey === correctKey) {
        statusClass = "correct";
        statusText = "✓ Correct";
      } else if (userKey) {
        statusClass = "wrong";
        statusText = "✗ Wrong";
      }

      const statusEl = document.createElement("div");
      statusEl.className = `cbt-review-status ${statusClass}`;
      statusEl.textContent = statusText;
      optionsEl.appendChild(statusEl);
    }

    const options = question.options && typeof question.options === "object" ? question.options : null;

    if (options) {
      for (const key of Object.keys(options)) {
        const normKey = normalizeCbtAnswerKey(key);

        const optionEl = document.createElement("div");
        optionEl.className = "opt";
        optionEl.dataset.key = key;

        optionEl.innerHTML = `<b>${escapeHtml(key)}</b>. ${renderTextWithDiagrams(options[key], {
          question,
          mode: "question"
        })}`;

        if (reviewAnswer !== null) {
          if (normKey === correctKey && userKey === correctKey) {
            optionEl.classList.add("opt-correct");
          } else if (normKey === userKey && userKey !== correctKey) {
            optionEl.classList.add("opt-wrong");
          } else if (normKey === correctKey) {
            optionEl.classList.add("opt-missed");
          } else {
            optionEl.classList.add("disabled");
          }

          optionEl.setAttribute("aria-disabled", "true");
        } else if (readOnly) {
          if (normKey === userKey) optionEl.classList.add("selected");
          optionEl.classList.add("disabled");
          optionEl.setAttribute("aria-disabled", "true");
        } else {
          if (normKey === userKey) optionEl.classList.add("selected");

          optionEl.onclick = () => {
            const nextKey = userKey === normKey ? null : key;
            if (typeof onOptionSelect === "function") {
              onOptionSelect(nextKey, question, key);
            }
          };
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

// ====== CBT JAMB constants ======
const CBT_ENGLISH_SUBJECT = "Use of English";
const CBT_DURATION_MS = 120 * 60 * 1000; // 2 hours fixed — matches real JAMB
// ================================

function getCbtSubjects() {
  // Always English first, then slots 2–4 (skip empty)
  const slots = [
    els("cbtSubject2")?.value || "",
    els("cbtSubject3")?.value || "",
    els("cbtSubject4")?.value || "",
  ].filter(Boolean);
  return [CBT_ENGLISH_SUBJECT, ...slots];
}

async function populateCbtSubjectSelectors() {
  // Fetch all JAMB objective subjects from /filters, exclude Use of English (auto-included)
  const selects = ["cbtSubject2", "cbtSubject3", "cbtSubject4"].map(id => els(id)).filter(Boolean);
  if (!selects.length) return;

  let subjects = [];
  try {
    const r = await api("/filters?qtype=objective&exam=JAMB");
    if (r?.subjects && Array.isArray(r.subjects)) {
      subjects = r.subjects
        .map(s => String(s || "").trim())
        .filter(s => s && s !== CBT_ENGLISH_SUBJECT);
    }
  } catch (_) {}

  // Store full list on state so syncCbtSubjectSelectors can use it without re-fetching
  state.cbt.availableSubjects = subjects;

  // Initial population — then sync to enforce mutual exclusivity
  selects.forEach(sel => {
    sel.innerHTML = `<option value="">— Select subject —</option>`;
    subjects.forEach(s => {
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      sel.appendChild(opt);
    });
  });

  syncCbtSubjectSelectors();
}

function syncCbtSubjectSelectors() {
  // Rebuild each slot's options excluding subjects already chosen in the other slots.
  // Preserves the current selection in each slot if it's still valid.
  const ids = ["cbtSubject2", "cbtSubject3", "cbtSubject4"];
  const selects = ids.map(id => els(id)).filter(Boolean);
  const subjects = Array.isArray(state.cbt.availableSubjects) ? state.cbt.availableSubjects : [];

  // Snapshot current selections before rebuilding
  const current = selects.map(sel => sel.value);

  selects.forEach((sel, i) => {
    // Subjects chosen in the OTHER two slots
    const takenByOthers = new Set(
      current.filter((v, j) => j !== i && v)
    );

    const prev = current[i];
    sel.innerHTML = `<option value="">— Select subject —</option>`;
    subjects.forEach(s => {
      if (takenByOthers.has(s)) return; // hide subjects already picked elsewhere
      const opt = document.createElement("option");
      opt.value = s;
      opt.textContent = s;
      sel.appendChild(opt);
    });

    // Restore selection if still available
    if (prev && !takenByOthers.has(prev)) sel.value = prev;
    else sel.value = "";
  });

  updateCbtSetupMeta();
}

function updateCbtSetupMeta() {
  const metaEl = els("cbtSetupMeta");
  if (!metaEl) return;

  const subjects = getCbtSubjects();
  if (subjects.length <= 1) {
    metaEl.textContent = "Select your 3 subjects to begin.";
    return;
  }
  metaEl.textContent = `${subjects.length} subject(s) selected: ${subjects.join(" • ")}`;
}

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
  const ENGLISH_SUBJECT = CBT_ENGLISH_SUBJECT;
  const ENGLISH_MULTIPLIER = 100 / 60;
  const OTHER_MULTIPLIER = 2.5;

  const questions = Array.isArray(state.cbt.questions) ? state.cbt.questions : [];
  const totalQuestions = questions.length;
  let answeredQuestions = 0;
  let correctAnswers = 0;
  const bySubject = {};
  const byTopic = {}; // key: "Subject||Topic" → { subject, topic, correct, total }

  for (let index = 0; index < questions.length; index += 1) {
    const question = questions[index];
    const subject = String(question?.subject || "Unknown").trim();
    const topic = String(question?.topic || "").trim();
    const selected = normalizeCbtAnswerKey(getCbtSelectedAnswer(question, index));
    const expected = normalizeCbtAnswerKey(question?.answer);

    // bySubject
    if (!bySubject[subject]) bySubject[subject] = { correct: 0, total: 0, weightedScore: 0, weightedMax: 0 };
    const multiplier = subject === ENGLISH_SUBJECT ? ENGLISH_MULTIPLIER : OTHER_MULTIPLIER;
    bySubject[subject].total += 1;
    bySubject[subject].weightedMax += multiplier;

    // byTopic
    if (topic) {
      const topicKey = `${subject}||${topic}`;
      if (!byTopic[topicKey]) byTopic[topicKey] = { subject, topic, correct: 0, total: 0 };
      byTopic[topicKey].total += 1;
    }

    if (selected) answeredQuestions += 1;
    if (selected && expected && selected === expected) {
      correctAnswers += 1;
      bySubject[subject].correct += 1;
      bySubject[subject].weightedScore += multiplier;
      if (topic) {
        const topicKey = `${subject}||${topic}`;
        if (byTopic[topicKey]) byTopic[topicKey].correct += 1;
      }
    }
  }

  const jambScore = Math.round(
    Object.values(bySubject).reduce((sum, s) => sum + s.weightedScore, 0)
  );
  const jambScoreMax = 400;
  const wrongAnswers = Math.max(0, answeredQuestions - correctAnswers);
  const percentage = totalQuestions ? Math.round((correctAnswers / totalQuestions) * 100) : 0;

  return {
    totalQuestions,
    answeredQuestions,
    correctAnswers,
    wrongAnswers,
    score: correctAnswers,
    percentage,
    jambScore,
    jambScoreMax,
    bySubject,
    byTopic,
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
  if (badgeEl) badgeEl.textContent = String(result.jambScore);

  const filterBits = state.cbt.selectedSubjects?.length
    ? state.cbt.selectedSubjects
    : [state.filters.exam, state.filters.year, state.filters.subject].filter(Boolean);
  if (metaEl) metaEl.textContent = filterBits.length
    ? `Submitted: ${filterBits.join(" • ")}`
    : "Submitted CBT session.";

  if (statusEl) {
    const unanswered = Math.max(0, result.totalQuestions - result.answeredQuestions);
    statusEl.textContent = unanswered
      ? `${unanswered} question(s) left unanswered.`
      : "All questions were answered.";
  }

  // Task A — Subject performance bars
  const breakdownEl = els("resultSubjectBreakdown");
  if (breakdownEl && result.bySubject && Object.keys(result.bySubject).length) {
    const subjectEntries = Object.entries(result.bySubject);
    breakdownEl.innerHTML = `
      <h3 style="margin:0 0 14px;">Subject Performance</h3>
      ${subjectEntries.map(([subject, { correct, total, weightedScore, weightedMax }]) => {
        const jamb = Math.round(weightedScore * 10) / 10;
        const pct = weightedMax > 0 ? Math.round((weightedScore / weightedMax) * 100) : 0;
        const barColor = pct >= 70 ? "#34d399" : pct >= 50 ? "#fbbf24" : "#f87171";
        return `
          <div style="margin-bottom:16px;">
            <div style="display:flex; justify-content:space-between; align-items:baseline; margin-bottom:6px;">
              <span style="font-weight:600; font-size:14px;">${escapeHtml(subject)}</span>
              <span style="font-size:13px; color:#a5b4fc; font-variant-numeric:tabular-nums;">
                <strong>${jamb}</strong>/100 &nbsp;•&nbsp; ${correct}/${total} correct
              </span>
            </div>
            <div style="height:10px; border-radius:999px; background:rgba(255,255,255,0.07); overflow:hidden;">
              <div style="height:100%; width:${pct}%; background:${barColor}; border-radius:999px; transition:width 600ms ease;"></div>
            </div>
          </div>
        `;
      }).join("")}
    `;
    breakdownEl.hidden = false;
  }

  // Task B — Weak topics
  const weakTopicsEl = els("resultWeakTopics");
  if (weakTopicsEl && result.byTopic && Object.keys(result.byTopic).length) {
    const WEAK_THRESHOLD = 0.6;
    const weakTopics = Object.values(result.byTopic)
      .filter(t => t.total >= 2 && (t.correct / t.total) < WEAK_THRESHOLD)
      .sort((a, b) => (a.correct / a.total) - (b.correct / b.total));

    if (weakTopics.length) {
      const bySubjectMap = {};
      weakTopics.forEach(t => {
        if (!bySubjectMap[t.subject]) bySubjectMap[t.subject] = [];
        bySubjectMap[t.subject].push(t);
      });
      weakTopicsEl.innerHTML = `
        <h3 style="margin:0 0 4px;">⚠ Weak Areas</h3>
        <div class="hint" style="margin-bottom:14px;">Topics where you scored below 60% — focus your revision here.</div>
        ${Object.entries(bySubjectMap).map(([subject, topics]) => `
          <div style="margin-bottom:16px;">
            <div style="font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:0.06em; color:#a5b4fc; margin-bottom:8px;">${escapeHtml(subject)}</div>
            ${topics.map(t => {
              const pct = Math.round((t.correct / t.total) * 100);
              return `
                <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 12px; background:rgba(239,68,68,0.07); border:1px solid rgba(239,68,68,0.18); border-radius:10px; margin-bottom:6px;">
                  <span style="font-size:13px; font-weight:600;">${escapeHtml(t.topic)}</span>
                  <span style="font-size:12px; color:#fca5a5; font-variant-numeric:tabular-nums;">${t.correct}/${t.total} correct (${pct}%)</span>
                </div>
              `;
            }).join("")}
          </div>
        `).join("")}
      `;
      weakTopicsEl.hidden = false;
    } else {
      weakTopicsEl.hidden = true;
    }
  }

  // Task C — Time analysis
  const timeAnalysisEl = els("resultTimeAnalysis");
  if (timeAnalysisEl) {
    const timeMap = state.cbt.subjectTimeMap || {};
    const subjects = Object.keys(timeMap).filter(s => timeMap[s] > 0);
    if (subjects.length) {
      const totalMs = Object.values(timeMap).reduce((a, b) => a + b, 0);
      timeAnalysisEl.innerHTML = `
        <h3 style="margin:0 0 4px;">⏱ Time Analysis</h3>
        <div class="hint" style="margin-bottom:14px;">Time spent per subject during the session.</div>
        ${subjects.map(subject => {
          const ms = timeMap[subject];
          const mins = Math.floor(ms / 60000);
          const secs = Math.floor((ms % 60000) / 1000);
          const pct = totalMs > 0 ? Math.round((ms / totalMs) * 100) : 0;
          const timeStr = mins > 0 ? `${mins} min ${secs} sec` : `${secs} sec`;
          return `
            <div style="display:flex; justify-content:space-between; align-items:center; padding:8px 12px; background:rgba(99,102,241,0.07); border:1px solid rgba(99,102,241,0.15); border-radius:10px; margin-bottom:6px;">
              <span style="font-size:13px; font-weight:600;">${escapeHtml(subject)}</span>
              <span style="font-size:12px; color:#a5b4fc; font-variant-numeric:tabular-nums;">${timeStr} (${pct}%)</span>
            </div>
          `;
        }).join("")}
      `;
      timeAnalysisEl.hidden = false;
    } else {
      timeAnalysisEl.hidden = true;
    }
  }

  // Conversion nudge for free users
  const nudgeEl = els("cbtConversionNudge");
  if (nudgeEl) {
    if (!state.isPaid) {
      const freeYr = state.cbt.freeYear || state.freeYear || "";
      nudgeEl.textContent = freeYr
        ? `You completed the ${freeYr} free year. Unlock all years to keep improving.`
        : "You've completed your free CBT session. Unlock all years to keep improving.";
      nudgeEl.hidden = false;
    } else {
      nudgeEl.hidden = true;
    }
  }

  resultSection.hidden = false;
}

function submitCurrentCbtSession({ reason = "manual" } = {}) {
  if (!state.cbt.sessionReady || state.cbt.submitted) return;

  resetCbtQuestionFeedbackForm();
  setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
  flushSubjectTime();
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
  const hours   = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

function stopCbtTimer() {
  if (state.cbt.timerIntervalId) {
    clearInterval(state.cbt.timerIntervalId);
    state.cbt.timerIntervalId = null;
  }
}

function updateCbtTimerUi() {
  const ms = state.cbt.timeRemainingMs;
  const timeStr = formatCountdown(ms);
  const isRunning = state.cbt.sessionReady && !state.cbt.submitted;

  // Inline timer badge inside workspace
  const timerEl = els("cbtTimer");
  if (timerEl) {
    timerEl.textContent = timeStr;
    timerEl.classList.toggle("is-warning", ms > 0 && ms <= 15 * 60 * 1000);
    timerEl.classList.toggle("is-expired", ms <= 0);
  }

  // Sticky bar
  const stickyBar = els("cbtStickyBar");
  const stickyTime = els("cbtStickyTime");
  const stickyPos  = els("cbtStickyPosition");
  const stickySub  = els("cbtStickySubject");

  if (!stickyBar) return;

  const showSticky = isRunning && !!state.cbt.timerStartedAt;
  stickyBar.hidden = !showSticky;
  document.body.classList.toggle("cbt-sticky-active", showSticky);

  if (!showSticky) return;

  if (stickyTime) stickyTime.textContent = timeStr;

  const total = state.cbt.questions.length;
  const pos = state.cbt.currentIndex + 1;
  if (stickyPos) stickyPos.textContent = `Question ${pos} of ${total}`;

  const currentQ = state.cbt.questions[state.cbt.currentIndex];
  if (stickySub) stickySub.textContent = currentQ?.subject || "";

  stickyBar.classList.toggle("is-warning", ms > 5 * 60 * 1000 && ms <= 15 * 60 * 1000);
  stickyBar.classList.toggle("is-danger",  ms > 0 && ms <= 5 * 60 * 1000);
  stickyBar.classList.toggle("is-expired", ms <= 0);
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

function startCbtTimer() {
  stopCbtTimer();
  state.cbt.timerDurationMs = CBT_DURATION_MS;
  state.cbt.timeRemainingMs = CBT_DURATION_MS;
  state.cbt.timerStartedAt = Date.now();
  state.cbt.subjectEnteredAt = Date.now();
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
  const orientationEl = els("cbtOrientationLabel");
  const cbtSection = els("cbtSection");

  if (cbtSection) openCbtContainer();

  const total = state.cbt.questions.length;
  const question = total ? state.cbt.questions[state.cbt.currentIndex] : null;

  if (!workspace) return;

  if (!question) {
    workspace.hidden = true;
    setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
    resetCbtQuestionFeedbackForm();
    if (orientationEl) orientationEl.textContent = "—";
    renderCbtSubjectTabs();
    renderCbtQuestionPalette();
    updateCbtTimerUi();
    updateCbtSessionMeta();
    updateCbtNavButtons();
    return;
  }

  const isReviewMode = !!state.cbt.submitted;
  workspace.hidden = false;

  // Sync currentSubject with the actual question being shown
  const questionSubject = String(question.subject || "").trim();
  if (questionSubject && state.cbt.currentSubject !== questionSubject) {
    state.cbt.currentSubject = questionSubject;
  }

  // Orientation label: "Chemistry | Q3/40" using per-subject position
  if (orientationEl) {
    const subjectIndices = state.cbt.subjectMap[questionSubject] || [];
    const posInSubject = subjectIndices.indexOf(state.cbt.currentIndex) + 1;
    const subjectTotal = subjectIndices.length;
    orientationEl.textContent = posInSubject > 0
      ? `${questionSubject} | Q${posInSubject}/${subjectTotal}`
      : `${questionSubject} | Q${state.cbt.currentIndex + 1}/${total}`;
  }

  // Update sticky bar position label too
  const stickyPos = els("cbtStickyPosition");
  if (stickyPos) {
    const subjectIndices = state.cbt.subjectMap[questionSubject] || [];
    const posInSubject = subjectIndices.indexOf(state.cbt.currentIndex) + 1;
    const subjectTotal = subjectIndices.length;
    stickyPos.textContent = posInSubject > 0
      ? `Q${posInSubject}/${subjectTotal}`
      : `Q${state.cbt.currentIndex + 1}/${total}`;
  }

  state.cbt.selectedOptionKey = getCbtSelectedAnswer(question, state.cbt.currentIndex);
  if (!state.cbt.feedbackOpen || isReviewMode) {
    resetCbtQuestionFeedbackForm();
    setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
  }
  renderQuestionInto("cbt", question, {
    selectedOptionKey: state.cbt.selectedOptionKey,
    readOnly: isReviewMode,
    reviewAnswer: isReviewMode ? normalizeCbtAnswerKey(question.answer) : null,
    stripQuestionNumber: true,
    questionNumber: (() => {
      const subjectIndices = state.cbt.subjectMap[questionSubject] || [];
      const pos = subjectIndices.indexOf(state.cbt.currentIndex) + 1;
      return pos > 0 ? pos : state.cbt.currentIndex + 1;
    })(),
    stripSectionRange: true,
    onOptionSelect: (nextKey, activeQuestion) => {
      if (isReviewMode) return;
      setCbtSelectedAnswer(activeQuestion, nextKey, state.cbt.currentIndex);
      renderCbtQuestion();
    },
  });

  // In review mode, auto-show explanation below the workspace
  const existingExpl = els("cbtReviewExplanation");
  if (existingExpl) existingExpl.remove();
  const existingToggle = els("btnCbtToggleExplanation");
  if (existingToggle) existingToggle.remove();

  if (isReviewMode) {
    const explHtml = renderExplainBlock(question);
    const explDiv = document.createElement("div");
    explDiv.id = "cbtReviewExplanation";
    explDiv.className = "cbt-review-explanation";
    explDiv.innerHTML = `<div style="font-weight:700; margin-bottom:8px;">Explanation</div>${explHtml}`;
    if (workspace) workspace.appendChild(explDiv);
  }

  // Render subject tabs and question palette (updates active state and answer counts)
  renderCbtSubjectTabs();
  renderCbtQuestionPalette();

  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateCbtNavButtons();

  const reportBtn = els("btnCbtReportQuestion");
  if (reportBtn) reportBtn.disabled = isReviewMode;

  const statusBits = [];
  if (isReviewMode) statusBits.push("Review mode — answers are locked after submission.");
  if (state.cbt.result) statusBits.push(`Score: ${state.cbt.result.jambScore}/${state.cbt.result.jambScoreMax}`);
  if (statusBits.length) setCbtStatus(statusBits.join(" "), isReviewMode ? "ok" : "");
}

// ====== CBT Subject & Question Navigation ======

function buildCbtSubjectMap() {
  // Groups question indices by subject, preserving load order.
  const map = {};
  state.cbt.questions.forEach((q, idx) => {
    const subj = String(q.subject || "Unknown").trim();
    if (!map[subj]) map[subj] = [];
    map[subj].push(idx);
  });
  return map;
}

function goToCbtQuestion(index) {
  const total = state.cbt.questions.length;
  if (index < 0 || index >= total) return;
  state.cbt.currentIndex = index;
  // Update currentSubject if crossing into a different subject's range
  const question = state.cbt.questions[index];
  if (question) {
    const subj = String(question.subject || "").trim();
    if (subj) state.cbt.currentSubject = subj;
  }
  renderCbtQuestion();
}

function flushSubjectTime() {
  const subject = state.cbt.currentSubject;
  const enteredAt = state.cbt.subjectEnteredAt;
  if (!subject || !enteredAt) return;
  const elapsed = Date.now() - enteredAt;
  if (!state.cbt.subjectTimeMap[subject]) state.cbt.subjectTimeMap[subject] = 0;
  state.cbt.subjectTimeMap[subject] += elapsed;
  state.cbt.subjectEnteredAt = Date.now();
}

function switchCbtSubject(subject) {
  const indices = state.cbt.subjectMap[subject];
  if (!indices || !indices.length) return;
  flushSubjectTime();
  state.cbt.currentSubject = subject;
  state.cbt.subjectEnteredAt = Date.now();
  state.cbt.currentIndex = indices[0];
  renderCbtQuestion();
}

function renderCbtSubjectTabs() {
  const tabBar = els("cbtSubjectTabs");
  if (!tabBar) return;

  const subjects = Object.keys(state.cbt.subjectMap);
  if (!subjects.length) {
    tabBar.hidden = true;
    return;
  }

  tabBar.hidden = false;
  tabBar.innerHTML = "";

  subjects.forEach(subject => {
    const indices = state.cbt.subjectMap[subject] || [];
    const answeredCount = indices.filter(i => {
      const q = state.cbt.questions[i];
      return q && getCbtSelectedAnswer(q, i);
    }).length;
    const total = indices.length;
    const isActive = subject === state.cbt.currentSubject;
    const allAnswered = answeredCount === total && total > 0;
    const someAnswered = answeredCount > 0;

    const tab = document.createElement("button");
    tab.type = "button";
    tab.className = [
      "cbt-tab",
      isActive ? "active" : "",
      allAnswered ? "all-answered" : someAnswered ? "some-answered" : "",
    ].filter(Boolean).join(" ");

    tab.setAttribute("aria-selected", isActive ? "true" : "false");
    tab.setAttribute("role", "tab");

    tab.innerHTML = `
      <span class="cbt-tab-name">${escapeHtml(subject)}</span>
      <span class="cbt-tab-count">${answeredCount}/${total}</span>
    `;

    tab.onclick = () => switchCbtSubject(subject);

    tabBar.appendChild(tab);
  });

  // Update palette whenever tabs re-render
  renderCbtQuestionPalette();
}

function renderCbtQuestionPalette() {
  const palette = els("cbtQuestionPalette");
  if (!palette) return;

  const subject = state.cbt.currentSubject;
  const indices = subject ? (state.cbt.subjectMap[subject] || []) : [];

  if (!indices.length) {
    palette.hidden = true;
    return;
  }

  palette.hidden = false;
  palette.innerHTML = "";

  const isReviewMode = !!state.cbt.submitted;

  indices.forEach((absoluteIndex, posInSubject) => {
    const question = state.cbt.questions[absoluteIndex];
    if (!question) return;

    const isCurrent = absoluteIndex === state.cbt.currentIndex;
    const selectedAnswer = getCbtSelectedAnswer(question, absoluteIndex);
    const hasAnswer = !!selectedAnswer;

    const btn = document.createElement("button");
    btn.type = "button";
    btn.setAttribute("aria-label", `Question ${posInSubject + 1}`);

    let stateClass = "";
    if (isReviewMode) {
      const expected = normalizeCbtAnswerKey(question.answer);
      const selected = normalizeCbtAnswerKey(selectedAnswer);
      if (!selected) {
        stateClass = "is-unanswered-review";
      } else if (selected === expected) {
        stateClass = "is-correct";
      } else {
        stateClass = "is-wrong";
      }
    } else {
      stateClass = hasAnswer ? "is-answered" : "";
    }

    btn.className = ["cbt-palette-btn", isCurrent ? "is-current" : "", stateClass]
      .filter(Boolean).join(" ");

    btn.textContent = String(posInSubject + 1);
    btn.onclick = () => {
      resetCbtQuestionFeedbackForm();
      setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
      goToCbtQuestion(absoluteIndex);
    };

    palette.appendChild(btn);
  });
}

// ===============================================

async function loadCbtSession() {
  updateCbtSetupMeta();

  const subjects = getCbtSubjects();

  // Must have English + at least one more subject
  if (subjects.length < 2) {
    openCbtContainer();
    els("cbtWorkspace").hidden = true;
    setCbtStatus("Select at least 3 more subjects before starting CBT.", "bad");
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
  state.cbt.selectedSubjects = subjects;
  stopCbtTimer();
  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateCbtNavButtons();
  els("resultSection")?.setAttribute("hidden", "hidden");
  setCbtStatus(`Loading ${subjects.length} subject(s)…`, "ok");

  // Fetch all subjects in parallel from /cbt/questions
  const fetches = subjects.map(subject =>
    api(`/cbt/questions?subject=${encodeURIComponent(subject)}&exam=JAMB`)
      .then(r => ({ subject, r }))
      .catch(() => ({ subject, r: { ok: false, error: "Network error" } }))
  );

  const results = await Promise.all(fetches);
  state.cbt.loading = false;

  const loadedSubjects = [];
  const failedSubjects = [];
  let allQuestions = [];

  let cbtFreeYear = null;
  for (const { subject, r } of results) {
    if (r?.items && Array.isArray(r.items) && r.items.length > 0) {
      allQuestions = allQuestions.concat(r.items);
      loadedSubjects.push(`${subject} (${r.items.length}q)`);
      // Capture free_year from first successful response
      if (cbtFreeYear === null && r.free_year != null) cbtFreeYear = r.free_year;
    } else {
      failedSubjects.push(subject);
    }
  }

  state.cbt.freeYear = cbtFreeYear;
  state.cbt.questions = allQuestions;
  state.cbt.currentIndex = 0;
  state.cbt.selectedOptionKey = null;
  state.cbt.answersByQuestionKey = {};
  state.cbt.submitted = false;
  state.cbt.result = null;
  state.cbt.sessionReady = state.cbt.questions.length > 0;
  state.cbt.subjectMap = buildCbtSubjectMap();
  state.cbt.currentSubject = Object.keys(state.cbt.subjectMap)[0] || null;

  if (!state.cbt.sessionReady) {
    openCbtContainer();
    els("cbtWorkspace").hidden = true;
    setCbtStatus("No questions found for the selected subjects. Check your selection and try again.", "bad");
    updateCbtNavButtons();
    return;
  }

  const statusParts = [];
  if (loadedSubjects.length) statusParts.push(`Loaded: ${loadedSubjects.join(", ")}`);
  if (failedSubjects.length) statusParts.push(`No data yet: ${failedSubjects.join(", ")}`);
  statusParts.push("120-minute timer started.");

  startCbtTimer();
  setCbtStatus(statusParts.join(" • "), failedSubjects.length ? "ok" : "ok");
  renderCbtQuestion();
}

function moveCbtQuestion(step) {
  const total = state.cbt.questions.length;
  if (!total) return;
  const nextIndex = Math.max(0, Math.min(total - 1, state.cbt.currentIndex + step));
  if (nextIndex === state.cbt.currentIndex) return;
  resetCbtQuestionFeedbackForm();
  setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
  goToCbtQuestion(nextIndex);
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

// Fetch all years + free_year from the backend for the current exam/subject
async function fetchStudyYears({ exam = null, subject = null } = {}) {
  const params = new URLSearchParams();
  if (exam) params.set("exam", exam);
  if (subject) params.set("subject", subject);
  const qs = params.toString();
  return api(`/study/years${qs ? "?" + qs : ""}`, { method: "GET" });
}

// Populate the year <select> with lock icons on paid-only years.
// Free users see all years but locked ones trigger upgrade prompt on click.
function renderYearFilterWithLocks(yearSel, allYears, freeYear, isPaid) {
  if (!yearSel) return;
  const current = yearSel.value;
  yearSel.innerHTML = "";

  // "All years" option (only for paid users)
  const allOpt = document.createElement("option");
  allOpt.value = "";
  allOpt.textContent = isPaid ? "All years" : "All years";
  yearSel.appendChild(allOpt);

  for (const y of allYears) {
    const opt = document.createElement("option");
    opt.value = String(y);
    const isLocked = !isPaid && freeYear !== null && y !== freeYear;
    opt.textContent = isLocked ? `${y} 🔒` : String(y);
    opt.dataset.locked = isLocked ? "1" : "0";
    yearSel.appendChild(opt);
  }

  // Restore previous selection if valid
  const exists = Array.from(yearSel.options).some((o) => o.value === current);
  yearSel.value = exists ? current : "";
}

// Call after year filter changes — intercept locked year selection
function handleYearFilterClick(yearSel) {
  if (!yearSel) return;
  const sel = yearSel.options[yearSel.selectedIndex];
  if (sel && sel.dataset.locked === "1") {
    // Revert to free year
    const freeYearStr = state.freeYear ? String(state.freeYear) : "";
    yearSel.value = freeYearStr;
    state.filters.year = freeYearStr;
    showUpgradePrompt("Unlock all years with a Core or Founding subscription.");
    return true; // was locked
  }
  return false;
}

function showUpgradePrompt(message) {
  // Show paywall panel with custom message, or fall back to status
  const pw = els("paywall");
  if (pw) {
    const pwText = pw.querySelector(".paywall-text");
    if (pwText && message) pwText.textContent = message;
    pw.removeAttribute("hidden");
    pw.classList.add("is-open");
    pw.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } else {
    setStatus(message || "Upgrade to access all years.", "bad");
  }
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
  const subjects = ["", ...data.subjects.map(String)];

  fillSelect(examSel, exams);
  fillSelect(subjSel, subjects);

  // Year filter: fetch free_year from /study/years and render with locks
  try {
    const yearsData = await fetchStudyYears({
      exam: exam ?? prev.exam ?? null,
      subject: prev.subject ?? null,
    });
    if (yearsData?.ok && Array.isArray(yearsData.years)) {
      state.freeYear = yearsData.free_year ?? null;
      const isPaid = yearsData.is_paid ?? state.isPaid;
      renderYearFilterWithLocks(yearSel, yearsData.years, state.freeYear, isPaid);
    } else {
      // Fallback to plain years from filters
      const years = ["", ...data.years.map((y) => String(y))];
      fillSelect(yearSel, years);
    }
  } catch {
    const years = ["", ...data.years.map((y) => String(y))];
    fillSelect(yearSel, years);
  }

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
    updateStudyMetaUI();
    maybeAutoLoadAfterFilterChange();
  };

  yearSel.onchange = async () => {
    // Intercept locked year — revert and show upgrade prompt
    if (handleYearFilterClick(yearSel)) return;
    save();
    await refreshFilterOptions({
      exam: state.filters.exam || undefined,
      year: state.filters.year ? parseInt(state.filters.year, 10) : undefined,
      keepSelection: true
    });
    updateStudyMetaUI();
    maybeAutoLoadAfterFilterChange();
  };

  subjSel.onchange = () => {
    save();
    updateStudyMetaUI();
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
      updateStudyMetaUI();
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

function buildFilterQuery() {
  const params = new URLSearchParams();
  if (state.filters.exam) params.set("exam", state.filters.exam);
  if (state.filters.year) params.set("year", state.filters.year);
  if (state.filters.subject) params.set("subject", state.filters.subject);
  const qs = params.toString();
  return qs ? `&${qs}` : "";
}

// ====== List ======
function renderList(items) {
  const list = els("list");
  if (!list) return;
  list.innerHTML = "";

  currentListIds = (items || []).map(x => x.id).filter(Boolean);
  if (!items || !items.length) {
    list.innerHTML = `<div class="status">No items returned. Try a smaller offset or clear filters.</div>`;
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

`;


    list.appendChild(div);
  }

  // restore highlight + visibility if a question is already selected
  if (activeQuestionId) {
    highlightQuestionCard(activeQuestionId);
    requestAnimationFrame(() => ensureActiveCardVisibleInList(activeQuestionId));
  }
}


async function openQuestion(id) {
  const requestSeq = ++studyOpenRequestSeq;

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
    if (requestSeq !== studyOpenRequestSeq) return;
    if (!q?.id) throw new Error(q?.error || "Question not found.");

    // ✅ Keep current question in state so Reveal/Explain (wired once in init) can use it
    state.currentQuestion = q;

    els("viewer").hidden = false;
    els("qTitle").textContent = id;

    focusViewer();

    // Study meta: clean "JAMB 2025 • Chemistry" format only
    const metaParts = [
      [q.exam, q.year].filter(Boolean).join(" "),
      q.subject,
    ].filter(Boolean);
    els("qMeta").textContent = metaParts.join(" • ");

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
        d.innerHTML = `<b>${escapeHtml(k)}</b>. ${renderTextWithDiagrams(q.options[k], { question: q, mode: "question" })}`;

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
  studyOpenRequestSeq += 1;
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

// ====== CBT Container — sole show/hide authority for the CBT lane ======
// Never toggle #cbtSection or #resultSection visibility directly outside these two functions.

function resetCbtState() {
  // Stop timer first — must happen before clearing state
  stopCbtTimer();

  // Reset all CBT state to clean initial values
  state.cbt.loading = false;
  state.cbt.questions = [];
  state.cbt.currentIndex = 0;
  state.cbt.selectedOptionKey = null;
  state.cbt.answersByQuestionKey = {};
  state.cbt.sessionReady = false;
  state.cbt.timerDurationMs = 0;
  state.cbt.timeRemainingMs = 0;
  state.cbt.timerStartedAt = 0;
  state.cbt.timerExpired = false;
  state.cbt.submitted = false;
  state.cbt.result = null;
  state.cbt.feedbackOpen = false;
  state.cbt.selectedSubjects = [];
  state.cbt.currentSubject = null;
  state.cbt.subjectMap = {};
  state.cbt.subjectTimeMap = {};
  state.cbt.subjectEnteredAt = 0;

  // Reset subject selector dropdowns
  ["cbtSubject2", "cbtSubject3", "cbtSubject4"].forEach(id => {
    const sel = els(id);
    if (sel) sel.value = "";
  });

  // Reset UI elements
  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateCbtNavButtons();
  updateCbtSetupMeta();

  // Hide workspace, palette, tabs and result; reset feedback panel
  const workspace = els("cbtWorkspace");
  if (workspace) workspace.hidden = true;
  const palette = els("cbtQuestionPalette");
  if (palette) palette.hidden = true;
  const tabBar = els("cbtSubjectTabs");
  if (tabBar) tabBar.hidden = true;
  const resultSection = els("resultSection");
  if (resultSection) resultSection.hidden = true;
  resetCbtQuestionFeedbackForm();
  setCbtQuestionFeedbackPanelOpen(false, { resetStatus: true });
}
function openCbtContainer() {
  const container = els("cbtContainer");
  if (!container) return;
  // Hide the study lane so it doesn't sit above CBT
  const studySection = els("studySection");
  if (studySection) studySection.hidden = true;
  // Close study viewer if open — CBT is a separate lane
  const viewer = els("viewer");
  if (viewer) viewer.hidden = true;
  container.hidden = false;
  container.scrollIntoView({ behavior: "smooth", block: "start" });
}

function closeCbtContainer() {
  const container = els("cbtContainer");
  if (!container) return;
  // Stop any running timer so it doesn't fire auto-submit after the UI is gone
  stopCbtTimer();
  container.hidden = true;
  // Restore the study lane
  const studySection = els("studySection");
  if (studySection) studySection.hidden = false;
}
// =======================================================================

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
  else if (!foundingOpen && !isFounding) payMessage = "Founding is full (500 students). Please use Core.";
  else if (isCoreActive) payMessage = "Core is active ✅ No renewal needed now.";
  else if (isActive && canRenewFounding) payMessage = "Founding access is active ✅ You can renew ₦1,000 to extend 1 year.";
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
  setDashboardMsg("You're ready. Start your JAMB CBT below.");
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

async function register(identifier, password, fullName = "") {
  saveApiBase();
  const r = await api("/auth/register", {
    method: "POST",
    body: JSON.stringify({ identifier, password, full_name: fullName || null })
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

  // ✅ Reset CBT session completely — no stale state after logout
  resetCbtState();
  closeCbtContainer();

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
  const fullName    = (els("regFullName")?.value || "").trim();
  const identifier  = (els("regIdentifier")?.value || "").trim();
  const password    = els("regPassword")?.value || "";
  const confirmPw   = els("regConfirmPassword")?.value || "";

  if (!fullName) {
    setAuthMsg("Please enter your full name.");
    els("regFullName")?.focus();
    return;
  }

  if (!identifier) {
    setAuthMsg("Please enter your phone number or email.");
    els("regIdentifier")?.focus();
    return;
  }

  if (password.length < 4) {
    setAuthMsg("Password must be at least 4 characters.");
    els("regPassword")?.focus();
    return;
  }

  if (password !== confirmPw) {
    setAuthMsg("Passwords do not match. Please try again.");
    els("regConfirmPassword")?.focus();
    return;
  }

  setAuthMsg("Creating account…");
  const r = await register(identifier, password, fullName);

  if (r?.token) {
    setAuthMsg("Account created ✅ You can now login.");
    showLoginView();
  } else {
    setAuthMsg(`Registration failed: ${r?.error || "unknown error"}`);
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

function showLoginView() {
  const registerView = els("registerView");
  if (registerView) registerView.style.display = "none";
  setAuthMsg("");
}

function showRegisterView() {
  const registerView = els("registerView");
  if (registerView) {
    registerView.style.display = "block";
    registerView.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  setAuthMsg("");
  requestAnimationFrame(() => els("regFullName")?.focus());
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

  // keep current list visible unless successful load
  const pw = els("paywall");
 if (pw) pw.classList.remove("is-open");

  setStatus("Loading…", "ok");
  state.paywalled = false;
  setListPagerUI({ loading: true });

  const filterQs = buildFilterQuery();
  const r = await api(`/questions/${mode}?limit=${limit}&offset=${offset}${filterQs}`);


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

  // v12.2: sort theory questions by numeric Q-number (Q1 < Q2 < Q10, not lexicographic)
  if (mode === "theory") {
    items.sort((a, b) => extractTheoryQNumber(a.id) - extractTheoryQNumber(b.id));
  }

  // success
  localStorage.setItem("ep_started", "1");
  state.pageIndex = pageIndex;
  state.lastItems = items;
 // ✅ Only use endReached heuristic for PAID users.
// For unpaid users, backend may clamp results (preview cap), but that doesn't mean "end".
  state.endReached = !!state.isPaid && (items.length < limit);

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

  // Hide JAMB CTA banner once logged in
  const jambCta = els("jambCtaSection");
  if (jambCta) jambCta.hidden = isLoggedIn;

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

  updateStudyMetaUI();
  updateCbtSetupMeta();
  updateCbtTimerUi();
  updateCbtSessionMeta();
  updateAdminUI();
  setListPagerUI({ loading: false });

  // Wire CBT result upgrade nudge button
  const btnCbtUpgradeNudge = els("btnCbtUpgradeNudge");
  if (btnCbtUpgradeNudge) {
    btnCbtUpgradeNudge.onclick = () => {
      const planSection = els("planSection") || els("dashboardSection");
      if (planSection) {
        planSection.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    };
  }

  // Never auto-load questions on page load — user must press Start Study.
  // For first-time users: show the start gate to guide them to pick filters.
  // For returning users with saved filters: just restore the filter state silently.
  if (isFirstTimeUser() && !filtersReady()) {
    setStartGateVisible(true);
  } else {
    setStartGateVisible(false);
  }

  // ✅ Safe event wiring (no null-crash)
  if (btnCheck) btnCheck.onclick = checkApi;

  const btnRegister = els("btnRegister");
  if (btnRegister) btnRegister.onclick = doRegister;

  const btnLogin = els("btnLogin");
  if (btnLogin) btnLogin.onclick = doLogin;

  const btnShowRegister = els("btnShowRegister");
  if (btnShowRegister) btnShowRegister.onclick = showRegisterView;

  const btnShowLogin = els("btnShowLogin");
  if (btnShowLogin) btnShowLogin.onclick = showLoginView;

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
  if (btnDashboardStartCbt) btnDashboardStartCbt.onclick = async () => {
    openCbtContainer();
    await populateCbtSubjectSelectors();
    updateCbtSetupMeta();
    setDashboardMsg("JAMB CBT session opened below.");
  };

  const btnCbtStartSession = els("btnCbtStartSession");
  if (btnCbtStartSession) btnCbtStartSession.onclick = () => {
    openCbtContainer();
    loadCbtSession();
  };

  const btnCbtReloadSession = els("btnCbtReloadSession");
  if (btnCbtReloadSession) btnCbtReloadSession.onclick = () => {
    openCbtContainer();
    loadCbtSession();
  };

  const btnCbtClose = els("btnCbtClose");
  if (btnCbtClose) {
    btnCbtClose.onclick = () => {
      // If a session is actively running (not yet submitted), warn the user
      if (state.cbt.sessionReady && !state.cbt.submitted) {
        const confirmed = window.confirm(
          "Close CBT session?\n\nThe timer will stop and your current session will be lost. This cannot be undone."
        );
        if (!confirmed) return;
      }
      resetCbtState();
      closeCbtContainer();
    };
  }

  // Wire subject slot selects → sync mutual exclusivity + update meta on change
  ["cbtSubject2", "cbtSubject3", "cbtSubject4"].forEach(id => {
    const sel = els(id);
    if (sel) sel.onchange = syncCbtSubjectSelectors;
  });

  const btnClose = els("btnClose");
  if (btnClose) btnClose.onclick = closeViewer;

  const btnStudy = els("btnStudy");
  if (btnStudy) btnStudy.onclick = () => {
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
  if (btnCbtSubmit) {
    btnCbtSubmit.onclick = () => {
      if (!state.cbt.sessionReady || state.cbt.submitted) return;

      const total = state.cbt.questions.length;
      const answered = Object.keys(state.cbt.answersByQuestionKey).length;
      const unanswered = Math.max(0, total - answered);

      if (unanswered > 0) {
        const confirmed = window.confirm(
          `You have ${unanswered} unanswered question(s) out of ${total}.\n\n` +
          `Click OK to submit and see your results.\n` +
          `Click Cancel to go back and complete your answers.`
        );
        if (!confirmed) return;
      }

      submitCurrentCbtSession({ reason: "manual" });
    };
  }

  const btnResultBackToCbt = els("btnResultBackToCbt");
  if (btnResultBackToCbt) {
    btnResultBackToCbt.onclick = () => {
      els("resultSection")?.setAttribute("hidden", "hidden");
      const workspace = els("cbtWorkspace");
      if (workspace) workspace.hidden = false;
      openCbtContainer();
      // Jump to first question of first subject for review
      const firstSubject = Object.keys(state.cbt.subjectMap)[0];
      if (firstSubject) switchCbtSubject(firstSubject);
      else renderCbtQuestion();
    };
  }

  const btnResultStartNew = els("btnResultStartNew");
  if (btnResultStartNew) {
    btnResultStartNew.onclick = async () => {
      updateCbtSetupMeta();
      await loadCbtSession();
      openCbtContainer();
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
  
function highlightStudyAnswers(question) {
  const optBox = els("qOptions");
  if (!optBox || !question?.options) return;

  const correctKey = normalizeCbtAnswerKey(question.answer);
  const userKey = normalizeCbtAnswerKey(selectedOptionKey);

  optBox.querySelectorAll(".opt").forEach(optEl => {
    const normKey = normalizeCbtAnswerKey(optEl.dataset.key);

    optEl.classList.remove("selected", "opt-correct", "opt-wrong", "opt-missed", "disabled");
    optEl.removeAttribute("aria-disabled");
    optEl.onclick = null;

    if (normKey === correctKey && userKey === correctKey) {
      optEl.classList.add("opt-correct");
    } else if (normKey === userKey && userKey !== correctKey) {
      optEl.classList.add("opt-wrong");
    } else if (normKey === correctKey) {
      optEl.classList.add("opt-missed");
    } else {
      optEl.classList.add("disabled");
    }
    optEl.setAttribute("aria-disabled", "true");
  });
}

const btnReveal = els("btnReveal");
  if (btnReveal) {
    btnReveal.onclick = () => {
      const q = state.currentQuestion;
      if (!q) return;

      const exp = els("qExplain");
      if (!exp) return;

      exp.hidden = false;
      exp.innerHTML = renderAnswerBlock(q);
      highlightStudyAnswers(q);
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

  // JAMB CTA button — scroll to login and focus identifier input
  const btnJambCtaLogin = els("btnJambCtaLogin");
  if (btnJambCtaLogin) {
    btnJambCtaLogin.onclick = () => {
      const accountSection = els("accountSection");
      if (accountSection) accountSection.scrollIntoView({ behavior: "smooth", block: "start" });
      requestAnimationFrame(() => {
        const identifierInput = els("identifier");
        if (identifierInput) identifierInput.focus();
      });
    };
  }

  // ✅ load founding cap + me before first render of upgrade UI
  await refreshFoundingStatus();
  await refreshMe();
  updateUpgradeUI(); // ensures btnPay hidden reflects cap immediately

  // ✅ Populate CBT subject selectors (needs auth state to be ready)
  await populateCbtSubjectSelectors();
  updateCbtSetupMeta();

}

init().catch((e)=>console.error(e));
