
// ExamPartner MVP client (auth + browse + Paystack upgrade) + filters + admin mini tools

const els = (id) => document.getElementById(id);
const apiBaseNoSlash = () => (state.apiBase || "").replace(/\/$/, "");
const FILTERS_PANEL_OPEN = "ep_filters_open";

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

function renderDiagrams(diagrams) {
  const box = els("qDiagrams");
  if (!box) return;
  box.innerHTML = "";
  if (!diagrams || !diagrams.length) return;

  for (const name of diagrams) {
    const img = document.createElement("img");
    img.loading = "lazy";
    img.alt = name;
    img.className = "diagram-img";
    img.src = `${apiBaseNoSlash()}/static/diagrams/${encodeURIComponent(name)}`;
    box.appendChild(img);
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
// ====================

// ---- Filter presets ----
const EXAM_OPTIONS = ["", "NECO", "WAEC", "JAMB"];
const SUBJECT_OPTIONS = ["", "Mathematics"];
const YEAR_OPTIONS = (() => {
  const now = new Date().getFullYear();
  const years = [""];
  for (let y = now; y >= 2000; y--) years.push(String(y));
  return years;
})();

// Admin key stored ONLY in sessionStorage
const ADMIN_KEY_STORAGE = "ep_admin_key";

const state = {
  apiBase: localStorage.getItem("apiBase") || "https://exampartner-backend.onrender.com",
  token: localStorage.getItem("token") || "",
  
  isPaid: false,
  authenticated: false,
  freeLimit: 10,
  busyPay: false,

  filters: {
    exam: localStorage.getItem("filter_exam") || "",
    year: localStorage.getItem("filter_year") || "",
    subject: localStorage.getItem("filter_subject") || "",
  },

  adminKey: sessionStorage.getItem(ADMIN_KEY_STORAGE) || "",
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
  chip.hidden = !state.isPaid;
}

function updatePracticeMetaUI() {
  const el = els("practiceMeta");
  if (!el) return;

  const exam = state.filters.exam || "All Exams";
  const subject = state.filters.subject || "All Subjects";
  year = state.filters.year || "All Years";
 els("practiceMeta").textContent = `${exam} • ${subject} • ${year}`;

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
  if (state.token) localStorage.setItem("token", state.token);
  else localStorage.removeItem("token");
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;",
  }[c]));
}

function trimText(s, n = 140) {
  s = (s || "").trim();
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}

function isEmail(v) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(v || "");
}

async function api(path, opts = {}) {
  const url = `${state.apiBase.replace(/\/$/, "")}${path}`;
  const headers = opts.headers ? { ...opts.headers } : {};
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  if (!headers["Content-Type"] && opts.method && opts.method !== "GET") {
    headers["Content-Type"] = "application/json";
  }

  const res = await fetch(url, { ...opts, headers });
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

  examSel.onchange = () => { save(); updatePracticeMetaUI(); };
  yearSel.onchange = () => { save(); updatePracticeMetaUI(); };
  subjSel.onchange = () => { save(); updatePracticeMetaUI(); };

  const btnClear = els("btnClearFilters");
  if (btnClear) {
    btnClear.onclick = () => {
      examSel.value = "";
      yearSel.value = "";
      subjSel.value = "";
      save();
      updatePracticeMetaUI();
      setStatus("Filters cleared.", "ok");
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

    <div class="qtext">${escapeHtml(trimText(q.question_text, 140))}</div>

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
    els("qText").textContent = q.question_text || "";
    renderDiagrams(q.diagrams || []);


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
        //   toggle OFF
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
updatePrevNextButtons();       els("btnReveal").onclick = () => {
      const exp = els("qExplain");
      exp.hidden = false;

       exp.innerHTML = `<div><b>Answer:</b> ${escapeHtml(q.answer || "—")}</div>`;

      // ✅ auto-scroll so user sees it immediately
      scrollToExplainBox();
     };


      els("btnExplain").onclick = () => {
      const exp = els("qExplain");
      exp.hidden = false;

      const pieces = [];
      if (q.explanation) pieces.push(`<div><b>Explanation:</b><br>${escapeHtml(q.explanation)}</div>`);
      if (q.solution_steps) pieces.push(`<div><b>Steps:</b><br>${escapeHtml(JSON.stringify(q.solution_steps, null, 2))}</div>`);
      if (q.sub_questions) pieces.push(`<div><b>Sub-questions:</b><br>${escapeHtml(JSON.stringify(q.sub_questions, null, 2))}</div>`);

      exp.innerHTML = pieces.length ? pieces.join("<hr/>") : `<div>No explanation/steps available.</div>`;

      // ✅ auto-scroll so user sees it immediately
      scrollToExplainBox();
     };

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

async function refreshMe() {
  if (!state.token) return;
  const r = await api("/me");
  if (r?.identifier) {
    state.authenticated = true;
    setPaidChip(r.is_paid);
    els("btnLogout").hidden = false;
    setAuthMsg(`Logged in as: ${r.identifier}`);
  } else {
    state.authenticated = false;
    setPaidChip(false);
    els("btnLogout").hidden = true;
  }
  updateUpgradeUI();
  updateAdminUI();
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
  saveToken("");
  state.authenticated = false;
  setPaidChip(false);
  setAuthMsg("Logged out.");
  els("btnLogout").hidden = true;

  adminClearKey();
  updateUpgradeUI();
  updateAdminUI();
}

async function loadList() {
  saveApiBase();
  const mode = els("mode").value;
  const offset = parseInt(els("offset").value || "0", 10) || 0;

  els("paywall").hidden = true;
  setStatus("Loading…", "ok");

  const filterQs = buildFilterQuery();
  const r = await api(`/questions/${mode}?limit=20&offset=${offset}${filterQs}`);

  if (r?.paywall) {
    setStatus("Preview limit reached. Please upgrade.", "bad");
    els("list").innerHTML = "";
    els("paywall").hidden = false;
    return;
  }

  renderList(r.items);
  setStatus(`Loaded ${r.items?.length || 0} items.`, "ok");
}

function updateUpgradeUI() {
  const btnPay = els("btnPay");
  const btnCheckPaid = els("btnCheckPaid");

  if (state.busyPay) {
    btnPay.disabled = true;
    btnCheckPaid.disabled = true;
    return;
  }

  btnPay.disabled = !state.authenticated || state.isPaid;
  btnCheckPaid.disabled = !state.authenticated;

  if (!state.authenticated) setPayMsg("Login to upgrade.");
  else if (state.isPaid) setPayMsg("You are already paid ✅");
  else setPayMsg("");
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

async function startPaystackPayment() {
  if (!state.authenticated) {
    setStatus("Please login before paying.", "bad");
    setPayMsg("Login to upgrade.");
    return;
  }

  const email = els("identifier").value.trim().toLowerCase();
  if (!isEmail(email)) {
    setStatus("Paystack requires an email. Please login/register with an email to pay.", "bad");
    setPayMsg("Use an email address to pay with Paystack.");
    return;
  }

  if (!window.PaystackPop || typeof window.PaystackPop.setup !== "function") {
    setStatus("Paystack script not loaded. Check inline.js in index.html", "bad");
    setPayMsg("Paystack failed to load. Check your internet connection and reload.");
    return;
  }

  setPayBusy(true, "Opening Paystack…");

  try {
    const pk = await getPaystackPublicKeyOrThrow();
    const amount = PAYSTACK_AMOUNT_NGN * 100;

    const identifier = email;

    const handler = window.PaystackPop.setup({
      key: pk,
      email: email,
      amount: amount,
      currency: PAYSTACK_CURRENCY,
      ref: "EP_" + Date.now(),
      metadata: { identifier, app: "ExamPartner" },

      callback: function (resp) {
        (async () => {
          const reference = resp?.reference;
          if (!reference) {
            setPayBusy(false, "");
            setStatus("Payment returned no reference. Please try again.", "bad");
            return;
          }

          setPayBusy(true, "Verifying payment…");
          const vr = await verifyPayment(reference, email);

          if (!vr?.ok) {
            setPayBusy(false, "");
            setStatus(`Payment received but verification failed: ${vr?.error || "unknown"}`, "bad");
            setPayMsg(`Ref: ${reference} (not verified)`);
            return;
          }

          await refreshMe();
          setPayBusy(false, "");
          setStatus("Payment verified ✅", "ok");
          setPayMsg(`Paid ✅ Ref: ${reference}`);
        })().catch((e) => {
          setPayBusy(false, "");
          setStatus(`Pay verify error: ${e?.message || e}`, "bad");
        });
      },

      onClose: function () {
        setPayBusy(false, "Payment cancelled.");
      },
    });

    handler.openIframe();
  } catch (e) {
    setPayBusy(false, "");
    setStatus(`Pay error: ${e?.message || e}`, "bad");
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
  if (!tools) return;
  tools.hidden = !state.adminKey;
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

// ====== Init ======
function init() {
  els("yr").textContent = new Date().getFullYear();
  els("apiBase").value = state.apiBase;

  

  initFiltersUI();

  // A2: remember filters panel open/closed
  const fp = els("filtersPanel");
  if (fp) {
    const saved = localStorage.getItem(FILTERS_PANEL_OPEN);
    if (saved === "1") fp.open = true;

    fp.addEventListener("toggle", () => {
     localStorage.setItem(FILTERS_PANEL_OPEN, fp.open ? "1" : "0");
   });
 }

  updatePracticeMetaUI();
  updateAdminUI();

  els("btnCheck").onclick = checkApi;
  els("btnRegister").onclick = doRegister;
  els("btnLogin").onclick = doLogin;
  els("btnLogout").onclick = doLogout;

  els("btnLoad").onclick = loadList;
  els("btnClose").onclick = closeViewer;

  const btnPractice = els("btnPractice");
  if (btnPractice) btnPractice.onclick = () => {
    const off = els("offset");
    if (off) off.value = 0;
    loadList();
    // bring the list into view on mobile
    const list = els("list");
    if (list) list.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  els("btnPay").onclick = startPaystackPayment;
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

  refreshMe();
}

init();
