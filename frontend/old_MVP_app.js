

// ExamPartner MVP client (auth + browse + Paystack upgrade) + filters + admin mini tools

const els = (id) => document.getElementById(id);
const apiBaseNoSlash = () => (state.apiBase || "").replace(/\/$/, "");
const FILTERS_PANEL_OPEN = "ep_filters_open";
const FILTER_CACHE_KEY = "ep_filter_cache_v1";
const FILTER_CACHE_TTL_MS = 24 * 60 * 60 * 1000; // 24 hours

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

function createDiagramImage(name, extraClass = "") {
  const img = document.createElement("img");
  img.loading = "lazy";
  img.alt = name;
  img.className = `diagram-img ${extraClass}`.trim();
  img.src = diagramSrc(name);
  return img;
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
      return `<img 
                class="diagram-img inline-diagram" 
                loading="lazy" 
                alt="${escapeHtml(name)}" 
                src="${src}">
              `;
    })
    .join("");

  return `<div class="subq-diagrams">${imgs}</div>`;
}

// Escape + preserve line breaks + allow explicit diagram placeholders:
// Use: [[diagram:FILE.png]] anywhere in question_text / explanation / steps text

function renderTextWithDiagrams(rawText, ctx = {}) {
  const safe = escapeHtml(String(rawText || ""));
  const withBreaks = safe.replace(/\n/g, "<br>");

  const tables = ctx.tables || state.currentQuestion?.tables || {};
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
    return `<div class="diagrams"><img loading="lazy" alt="${alt}" class="diagram-img inline-diagram" src="${src}"></div>`;
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
  devMode: false,
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

function renderSolutionSteps(steps) {
  if (!steps) return "";
  // steps can be string, array, or object
  if (typeof steps === "string") return `<div>${renderTextWithDiagrams(steps)}</div>`;
  if (Array.isArray(steps)) {
    const items = steps
      .map((s) => {
        if (typeof s === "string") return `<li>${renderTextWithDiagrams(s)}</li>`;
        // objects: show JSON safely
        return `<li>${escapeHtml(JSON.stringify(s))}</li>`;
      })
      .join("");
    return `<ol style="margin:6px 0 0 18px;">${items}</ol>`;
  }
  return `<pre style="white-space:pre-wrap;margin:6px 0 0;">${escapeHtml(JSON.stringify(steps, null, 2))}</pre>`;
}

  function renderExplanation(explanationArray) {
  if (!Array.isArray(explanationArray)) {
    return renderTextWithDiagrams(String(explanationArray || ""), { mode: "explain" });
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

 function renderSubQuestions(items, opts = {}) {
  const showAnswers = opts.showAnswers !== false;              // default true
  const showExplanations = opts.showExplanations !== false;    // default true
  const showDiagrams = opts.showDiagrams !== false;            // default true

  // ✅ NEW (tables context + mode)
  // mode: "question" | "reveal" | "explain"
  const tables = opts.tables || (state && state.currentQuestion ? state.currentQuestion.tables : {}) || {};
  const mode = opts.mode || "question";

  if (!items) return "";
  if (!Array.isArray(items)) {
    return `<pre style="white-space:pre-wrap;margin:6px 0 0;">${escapeHtml(JSON.stringify(items, null, 2))}</pre>`;
  }

  const renderNode = (n) => {
    if (!n || typeof n !== "object") return "";

    const label = n.label ? `<b>${escapeHtml(String(n.label))}</b> ` : "";

    // ✅ NEW: renderTextWithDiagrams now receives { tables, mode }
      const subqText = n.question_text || n.text || "";
     const text = subqText
     ? `${renderTextWithDiagrams(String(subqText), { tables, mode })}`
     : "";

    // Subquestion diagrams (question-phase diagrams)
    const qDiagrams = (showDiagrams && Array.isArray(n.diagrams) && n.diagrams.length)
      ? renderDiagramsHtml(n.diagrams)
      : "";

    // ✅ NEW: answer uses reveal mode (so [[table:T1:answer]] can work if you use it)
    const answer = (showAnswers && n.answer)
      ? `<div style="margin-top:8px;"><b>Answer:</b> ${renderTextWithDiagrams(String(n.answer), { tables, mode: "reveal" })}</div>`
      : "";

    // Answer diagrams (Reveal)
    const aDiagrams = (showAnswers && showDiagrams && Array.isArray(n.answer_diagrams) && n.answer_diagrams.length)
      ? renderDiagramsHtml(n.answer_diagrams)
      : "";

    // ✅ NEW: explanation uses explain mode (so [[table:T1]] placeholders render as explanation context if needed)
      const explanation = (showExplanations && n.explanation)
      ? `<div style="margin-top:8px;">
          <b>Explanation:</b>
         <div style="margin-top:6px;">
         ${renderExplanation(n.explanation)}
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


// ---- Viewer section builders (new flow) ----
function renderQuestion(q) {
  const qTextEl = els("qText");

  if (qTextEl) {
    qTextEl.innerHTML = `<div>${renderTextWithDiagrams(q.question_text || "", { tables: q.tables || {}, mode: "question" })}</div>`;
  }

  const qTablesEl = els("qTables");
  if (qTablesEl) {
    qTablesEl.innerHTML = "";

    // Only render tables referenced directly in the MAIN question_text
    const rawRefs = (String(q.question_text || "").match(/\[\[table:([^\]]+)\]\]/gi) || []);
    const tableRefs = rawRefs
      .map(x => x.replace(/\[\[table:|\]\]/gi, "").split(":")[0].trim())
      .filter(Boolean);

    if (tableRefs.length) {
      qTablesEl.hidden = false;
      renderTablesInto(qTablesEl, q.tables || {}, tableRefs, "question");
    } else {
      qTablesEl.hidden = true;
    }
  }

  renderDiagramsInto(els("qDiagrams"), q.diagrams || [], { variant: "block" });

  const subBox = els("qSubQuestions");
  if (subBox) {
    if (q.sub_questions && Array.isArray(q.sub_questions) && q.sub_questions.length) {
      subBox.hidden = false;
      subBox.innerHTML = `
        <div style="font-weight:700; margin:12px 0 6px;">Sub-questions</div>
        ${renderSubQuestions(q.sub_questions, {
          showAnswers: false,
          showExplanations: false,
          showDiagrams: true,
          tables: q.tables || {},
          mode: "question"
        })}
      `;
    } else {
      subBox.hidden = true;
      subBox.innerHTML = "";
    }
  }

  const exp = els("qExplain");
  if (exp) {
    exp.hidden = true;
    exp.innerHTML = "";
  }
}

function renderAnswerBlock(q) {
  const parts = [];

  const mainAns = q.answer ? renderTextWithDiagrams(String(q.answer), { tables: q.tables, mode: "reveal" }) : "—";
  parts.push(`<div><b>Answer:</b> ${mainAns}</div>`);

  // Optional answer diagrams
  if (Array.isArray(q.answer_diagrams) && q.answer_diagrams.length) {
    parts.push(renderDiagramsHtml(q.answer_diagrams));
  }

  // Sub-question answers (if present)
  if (q.sub_questions) {
    const html = renderSubQuestions(q.sub_questions, { showAnswers: true, showExplanations: false, showDiagrams: true });
    if (html) parts.push(`<div style="margin-top:12px;"><b>Sub-question answers:</b>${html}</div>`);
  }

  return parts.join("<hr/>");
}

function renderExplainBlock(q) {
  const parts = [];

  if (q.explanation) {
    let explanationHtml = "";

    if (Array.isArray(q.explanation)) {
      // ✅ Objective → array → use renderExplanation
      explanationHtml = renderExplanation(q.explanation);
    } else {
      // ✅ Theory → string → normal rendering
      explanationHtml = renderTextWithDiagrams(String(q.explanation), {
        tables: q.tables,
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

  if (Array.isArray(q.explanation_diagrams) && q.explanation_diagrams.length) {
    parts.push(renderDiagramsHtml(q.explanation_diagrams));
  }

  if (q.solution_steps) {
    parts.push(`<div><b>Steps:</b>${renderSolutionSteps(q.solution_steps)}</div>`);
  }

  // For theory, this shows full tree (including answer/explanation + diagrams inside subquestions)
  if (q.sub_questions) {
    parts.push(`<div><b>Sub-questions:</b>${renderSubQuestions(q.sub_questions, { showAnswers: true, showExplanations: true, showDiagrams: true })}</div>`);
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

  try {
    const r = await api(path, { method: "GET" });

    // Expect { ok: true, exams, years, subjects }
    if (r?.ok && Array.isArray(r.exams)) {
      saveFilterCache(r);
      return r;
    }
  } catch (e) {
    console.warn("Filters API failed:", e);
  }

  // 🔁 fallback: last known good DB-driven filters only
  const cached = loadFilterCache();
 if (cached) {
  console.warn("Using cached filters");
  return cached;
}
return null;

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

  const data = await fetchFilters({
    qtype: qtypeParam,
    exam: exam ?? prev.exam ?? null,
    year: year ?? (prev.year ? parseInt(prev.year, 10) : null),
  });

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
    maybeAutoLoadAfterFilterChange();
  };

  subjSel.onchange = () => {
    save();
    updatePracticeMetaUI();
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
      if (isFirstTimeUser()) setStartGateVisible(true);
    };
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

  <div class="meta">${escapeHtml(meta.join(" • "))}</div>
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

    const q = await api(`/question/${encodeURIComponent(id)}`);

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
  els("viewer").hidden = true;
  setViewerOpen(false);
  clearQuestionHighlight();

  currentIndex = -1;
  updatePrevNextButtons();

if (els("qDiagrams")) els("qDiagrams").innerHTML = "";
  els("qOptions").innerHTML = "";
  els("qExplain").hidden = true;
  els("qExplain").innerHTML = "";
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


function updatePayEmailUI() {
  const label = els("payEmailLabel");   // optional
  const input = els("payEmailInput");   // required
  const hint  = els("payEmailHint");    // optional

  // If the input isn't in HTML, we can't show anything
  if (!input) return;

  // Not logged in → never show
  if (!state.authenticated || !state.me) {
    if (label) label.hidden = true;
    input.hidden = true;
    if (hint) hint.hidden = true;
    input.value = "";
    return;
  }

  const identifier = (state.me.identifier || "").trim();
  const storedEmail = (state.me.email || "").trim();

  const needsEmail =
    !isValidEmail(identifier) &&
    !isValidEmail(storedEmail);

  if (label) label.hidden = !needsEmail;
  input.hidden = !needsEmail;
  if (hint) hint.hidden = !needsEmail;

  if (needsEmail) {
    // keep whatever user typed; if profile has email, prefill
    if (isValidEmail(storedEmail) && !input.value) input.value = storedEmail;
  } else {
    input.value = "";
  }
}

  async function refreshMe() {
  // 🔒 Always re-hydrate token first
  state.token = sessionStorage.getItem("token") || "";

  // Helper: fully reset auth-dependent UI/state
  function resetAuthUI() {
    state.authenticated = false;
    state.isPaid = false;
    state.justPaidAttempt = false;

    setPaidChip(false);

    const btnLogout = els("btnLogout");
    if (btnLogout) btnLogout.hidden = true;

    // Payment history toggle + box
    const btnHist = els("btnToggleHistory");
    if (btnHist) btnHist.hidden = true;

    const phBox = els("paymentHistory");
    if (phBox) phBox.hidden = true;

    state.historyOpen = false;
    state.historyLoadedOnce = false;

    // Hide paywall (animated system)
    const pw = els("paywall");
    if (pw) {
      pw.removeAttribute("hidden");
      pw.classList.remove("is-open");
    }

    updatePayEmailUI();
  }

  if (!state.token) {
    resetAuthUI();
    updateUpgradeUI();
    updateAdminUI();
    return;
  }

  const wasPaid = !!state.isPaid;

  const r = await api("/me");

  if (r?.identifier) {
    state.authenticated = true;

    // ✅ canonical profile state (matches your backend /me response)
     state.me = {
        identifier: String(r.identifier || "").trim(),
        email: String(r.email || "").trim(),
        isPaid: !!r.is_paid,
        isPaidActive: (r.is_paid_active !== undefined) ? !!r.is_paid_active : !!r.is_paid,
        plan: String(r.plan || "free"),
        isFounding: !!r.is_founding,
        paidUntil: r.paid_until ? String(r.paid_until) : "",
     };

     const nowPaid = !!state.me.isPaidActive;   // ✅ use active status (paid_until > now)
     state.isPaid = nowPaid;
     if (state.isPaid) state.justPaidAttempt = false;



    const btnLogout = els("btnLogout");
    if (btnLogout) btnLogout.hidden = false;

    setAuthMsg(`Logged in as: ${state.me.identifier}`);

    // ✅ Update email UI visibility (payEmailInput system)
    updatePayEmailUI();

    // Payment history toggle (Upgrade panel)
    const btnHist = els("btnToggleHistory");
    if (btnHist) {
      btnHist.hidden = false;
      btnHist.textContent = state.historyOpen
        ? "Hide payment history"
        : "View payment history";
    }

    const phBox = els("paymentHistory");
    if (phBox) phBox.hidden = !state.historyOpen;

    // Load only if already open
    if (state.historyOpen) loadPaymentHistory().catch(() => {});

    // ✅ keep session alive while user is active
    resetIdleTimer();

    // ✅ if user transitioned from unpaid -> paid, clear paywall + reload page 1
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
    // Token exists but /me failed (expired/invalid)
    resetAuthUI();
  }
  
   await refreshFoundingStatus();
  updateUpgradeUI();
  updatePlanMetaUI();
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
  saveApiBase();
  const identifier = els("identifier").value.trim();
  const password = els("password").value;

  setAuthMsg("Registering…");
  const r = await api("/auth/register", { method: "POST", body: JSON.stringify({ identifier, password }) });

  if (r?.token) {
    saveToken(r.token);
    setAuthMsg("Registered ✅");
    await refreshMe();
  } else {
    setAuthMsg(`Register failed: ${r?.error || "unknown error"}`);
  }
}

async function doLogin() {
  saveApiBase();
  const identifier = els("identifier").value.trim();
  const password = els("password").value;

  setAuthMsg("Logging in…");
  const r = await api("/auth/login", { method: "POST", body: JSON.stringify({ identifier, password }) });

  if (r?.token) {
    saveToken(r.token);
    setAuthMsg("Logged in ✅");
    await refreshMe();
  } else {
    setAuthMsg(`Login failed: ${r?.error || "unknown error"}`);
  }
}

 async function doLogout() {
  stopIdleTimer();
  saveToken("");

  state.authenticated = false;
  state.isPaid = false;        // ✅ important
  state.paywalled = false;     // ✅ reset
  state.endReached = false;    // ✅ reset
  state.pageIndex = 0;         // ✅ reset

  state.hasLoadedQuestions = false;


  setPaidChip(false);
  setAuthMsg("Logged out.");
  const btn = els("btnLogout");
  if (btn) btn.hidden = true;

  // Clear UI so next user doesn't see previous content
  const list = els("list");
  if (list) list.innerHTML = "";
  closeViewer?.();

  adminClearKey();
  updateUpgradeUI();
  updateAdminUI();
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

function updatePlanMetaUI() {
  const box = els("planMeta");
  const badge = els("foundingBadge");
  const until = els("activeUntil");
  if (!box || !badge || !until) return;

  if (!state.authenticated) {
    box.hidden = true;
    return;
  }

  const isFounding = !!(state.me && state.me.isFounding);
  const paidUntil = (state.me && state.me.paidUntil) ? state.me.paidUntil : "";

  box.hidden = false;
  badge.hidden = !isFounding;

  if (!!state.isPaid && paidUntil) {
    const dleft = daysLeft(paidUntil);
    const dtext = (dleft !== null && dleft >= 0) ? ` (${dleft} day${dleft === 1 ? "" : "s"} left)` : "";
    until.textContent = `Active until ${fmtDate(paidUntil)}${dtext}`;
  } else {
    until.textContent = "";
  }
}

 function updateUpgradeUI() {
  const btnPay = els("btnPay");
  const btnPayCore = els("btnPayCore");
  const btnCheckPaid = els("btnCheckPaid");
  if (!btnPay || !btnCheckPaid) return;

  const foundingOffer = els("foundingOffer");

  const isActive = !!state.isPaid;
  const isFounding = !!(state.me && state.me.isFounding);
  const plan = (state.me && state.me.plan) ? state.me.plan : "free";
  const isCoreActive = isActive && plan === "core";

  // -----------------------------
  // ✅ Founding cap: hide ₦1,000 when cap is hit (except existing founders)
  // If you haven't wired state.foundingStatus yet, foundingOpen defaults to true.
  // -----------------------------
  const foundingOpen =
    (state.foundingStatus && typeof state.foundingStatus.open === "boolean")
      ? state.foundingStatus.open
      : true;

  const allowFoundingButton = state.authenticated && (foundingOpen || isFounding);

  // Show/hide ₦1,000 button
  btnPay.hidden = !allowFoundingButton;

  // Hide/show the Founding offer copy together with the ₦1,000 button
  if (foundingOffer) foundingOffer.hidden = btnPay.hidden;

  // ✅ Core button visibility rule
  // Show only when logged in AND Core not already active
  if (btnPayCore) {
    btnPayCore.hidden = !state.authenticated || isCoreActive;
  }

  // ✅ Upgrade hint: show only AFTER browsing starts, and only for unpaid logged-in users
  const upgradeHint = els("upgradeHint");
  if (upgradeHint) {
    upgradeHint.hidden = isActive || !state.authenticated || !state.hasLoadedQuestions;
  }

  // ✅ Busy-pay lock (while popup is opening / active)
  if (state.busyPay) {
    btnPay.disabled = true;
    if (btnPayCore) btnPayCore.disabled = true;
    btnCheckPaid.disabled = true;
    return;
  }

  // Allow founders to renew ₦1,000 even if currently active
  const canRenewFounding = isFounding; // keep it simple for MVP

  // Disable Pay ₦1,000 when:
  // - not logged in, OR
  // - user is active Core, OR
  // - user is active but NOT a founder (optional)
  btnPay.disabled = !state.authenticated || isCoreActive || (isActive && !canRenewFounding);

  // ✅ IMPORTANT FIX:
  // Re-enable Core button after busyPay ends (unless Core is already active / not logged in)
  if (btnPayCore) {
    btnPayCore.disabled = !state.authenticated || isCoreActive;
  }

  // -----------------------------
  // ✅ Refresh Paid Status button: show only when it’s useful
  // - logged in
  // - unpaid
  // - and either paywall reached OR user just attempted payment
  // -----------------------------
  const showRefresh =
    state.authenticated &&
    !isActive &&
    (state.paywalled || !!state.justPaidAttempt);

  btnCheckPaid.hidden = !showRefresh;
  btnCheckPaid.disabled = !state.authenticated;

  // Status message
  if (!state.authenticated) setPayMsg("Login to upgrade.");
  else if (!foundingOpen && !isFounding) setPayMsg("Founding is closed. Please use Core.");
  else if (isCoreActive) setPayMsg("Core is active ✅ No renewal needed now.");
  else if (isActive && canRenewFounding) setPayMsg("Founding access is active ✅ You can renew ₦1,000 to extend 30 days.");
  else if (isActive) setPayMsg("You are already paid ✅");
  else setPayMsg("");
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
  if (msg) setPayMsg(msg);
  updateUpgradeUI();
}

async function verifyPayment(reference, email) {
  return await api("/payments/verify", {
    method: "POST",
    body: JSON.stringify({ reference, email }),
  });
}


 async function startPaystackPayment(amountNgn = PAYSTACK_AMOUNT_NGN) {
  updatePayEmailUI();

  if (!state.authenticated || !state.me) {
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

  const identifier = String(state.me.identifier || els("identifier")?.value || "")
    .trim()
    .toLowerCase();

  let payEmail = "";

  if (isValidEmail(identifier)) {
    payEmail = identifier;
  } else if (isValidEmail(state.me.email || "")) {
    payEmail = String(state.me.email).trim().toLowerCase();
  } else {
    // HARD GATE: phone identifier must provide receipt email
    const input = els("payEmailInput");
    payEmail = String(input?.value || "").trim().toLowerCase();

    if (!isValidEmail(payEmail)) {
      updatePayEmailUI();
      if (input) {
        input.hidden = false;
        input.focus();
      }
      setStatus("Please enter a valid receipt email to continue.", "bad");
      setPayMsg("Receipt email is required for phone-number accounts.");
      return;
    }

    const up = await api("/me/email", {
      method: "POST",
      body: JSON.stringify({ email: payEmail }),
    });

    if (!up?.ok) {
      setStatus(`Could not save email: ${up?.error || "unknown"}`, "bad");
      setPayMsg("Please try again.");
      return;
    }

    state.me.email = payEmail;
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
            setPayBusy(false, "");
            setStatus("Payment returned no reference. Please try again.", "bad");
            // keep justPaidAttempt = true so Refresh can show
            updateUpgradeUI();
            return;
          }

          setPayBusy(true, "Verifying payment…");
          const vr = await verifyPayment(reference, payEmail);

          if (!vr?.ok) {
            setPayBusy(false, "");
            setStatus(`Payment received but verification failed: ${vr?.error || "unknown"}`, "bad");
            setPayMsg(`Ref: ${reference} (not verified)`);
            // keep justPaidAttempt = true so Refresh can show
            updateUpgradeUI();
            return;
          }

          await refreshMe(); // refreshMe will clear justPaidAttempt if user is now paid

          setPayBusy(false, "");
          setStatus("Payment verified ✅", "ok");
          setPayMsg(`Paid ✅ Ref: ${reference}`);
          updateUpgradeUI();
        })().catch((e) => {
          setPayBusy(false, "");
          setStatus(`Pay verify error: ${e?.message || e}`, "bad");
          // keep justPaidAttempt = true so Refresh can show
          updateUpgradeUI();
        });
      },

      onClose: function () {
        setPayBusy(false, "Payment cancelled.");
        // keep justPaidAttempt = true so Refresh can show
        updateUpgradeUI();
      },
    });

    handler.openIframe();
  } catch (e) {
    setPayBusy(false, "");
    setStatus(`Pay error: ${e?.message || e}`, "bad");
    // keep justPaidAttempt = true so Refresh can show
    updateUpgradeUI();
  }
}



async function checkPaidStatus() {
  await refreshMe();
  setStatus(state.isPaid ? "Paid ✅" : "Not paid yet.", state.isPaid ? "ok" : "bad");
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

  updatePracticeMetaUI();
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

  const btnLogout = els("btnLogout");
  if (btnLogout) btnLogout.onclick = doLogout;

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

  const btnPay = els("btnPay");
  if (btnPay) btnPay.onclick = () => startPaystackPayment(PAYSTACK_AMOUNT_NGN);

  const btnPayCore = els("btnPayCore");
  if (btnPayCore) btnPayCore.onclick = () => startPaystackPayment(PAYSTACK_CORE_AMOUNT_NGN);

  const btnCheckPaid = els("btnCheckPaid");
  if (btnCheckPaid) btnCheckPaid.onclick = checkPaidStatus;

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

  setupIdleTimeout();

  // ✅ load founding cap + me before first render of upgrade UI
  await refreshFoundingStatus();
  await refreshMe();
  updateUpgradeUI(); // ensures btnPay hidden reflects cap immediately

}

init().catch((e)=>console.error(e));
