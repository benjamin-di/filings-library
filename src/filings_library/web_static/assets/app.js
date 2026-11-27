// Filings Library — Read-Only Institutional Research Workspace (zero external runtime deps).

const state = {
  me: null,
  companies: [],
  view: "companies", // "companies" | "reader" | "xbrl"
  selectedCompany: null,
  familyFilter: "",
  companyDocs: [],
  // Reader state
  docId: null,
  docDetail: null,
  activeLocator: "",
  activeQuery: "",
  sectionData: null,
  readerMode: "structured", // "structured" | "raw"
  drawerTab: "redline", // "redline" | "xbrl"
  diffTargetId: null,
  diffChangesOnly: true,
  diffData: null,
  companyFacts: null,
  hitIndex: -1,
  // Command palette state
  cmdOpen: false,
  cmdItems: [],
  cmdSelected: 0,
  cmdTimer: null,
  // Async request sequence guards
  cmdSearchSeq: 0,
  companySeq: 0,
  openDocSeq: 0,
};

function esc(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  clearTimeout(showToast._t);
  showToast._t = setTimeout(() => el.classList.add("hidden"), 2200);
}

async function apiGet(path) {
  const res = await fetch(path, { credentials: "same-origin" });
  if (res.status === 401) {
    window.location.href = `/connections/login?next=${encodeURIComponent(window.location.pathname + window.location.search)}`;
    throw new Error("Unauthorized");
  }
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

function currencyPrefix(ccy) {
  if (ccy === "USD") return "$";
  if (ccy === "EUR") return "€";
  if (ccy === "GBP") return "£";
  if (ccy === "JPY" || ccy === "CNY") return "¥";
  if (ccy === "HKD") return "HK$";
  return ccy ? `${ccy} ` : "";
}

function formatNumber(val, unit = "") {
  if (typeof val !== "number" || Number.isNaN(val)) return "—";
  const sign = val < 0 ? "-" : "";
  const abs = Math.abs(val);
  if (unit.includes("/shares")) {
    const ccy = unit.split("/")[0].trim();
    return `${sign}${currencyPrefix(ccy)}${abs.toFixed(2)}`;
  }
  if (unit === "shares") {
    return val.toLocaleString("en-US", { maximumFractionDigits: 0 });
  }
  const prefix = currencyPrefix(unit.trim());
  if (abs >= 1e9) return `${sign}${prefix}${(abs / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${sign}${prefix}${(abs / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${sign}${prefix}${(abs / 1e3).toFixed(1)}K`;
  return `${sign}${prefix}${abs.toFixed(2)}`;
}

// Highlight meaningful query terms in raw (unescaped) text, then HTML-escape safely.
function highlightRaw(rawText, query) {
  const text = String(rawText ?? "");
  if (!query) return esc(text);
  const terms = [...new Set((query.match(/[A-Za-z0-9][A-Za-z0-9._-]{1,}/g) || []).map((t) => t.toLowerCase()))];
  if (!terms.length) return esc(text);
  const pattern = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  const parts = text.split(pattern);
  return parts
    .map((part, idx) => (idx % 2 === 1 ? `<mark class="hit">${esc(part)}</mark>` : esc(part)))
    .join("");
}

function splitMarkdownRow(rowLine) {
  const trimmed = rowLine.trim().replace(/^\|/, "").replace(/(?<!\\)\|$/, "");
  const cells = [];
  let cur = "";
  for (let idx = 0; idx < trimmed.length; idx += 1) {
    const ch = trimmed[idx];
    if (ch === "\\" && idx + 1 < trimmed.length && trimmed[idx + 1] === "|") {
      cur += "|";
      idx += 1;
    } else if (ch === "|") {
      cells.push(cur.trim());
      cur = "";
    } else {
      cur += ch;
    }
  }
  cells.push(cur.trim());
  return cells;
}

// Safe Markdown -> HTML renderer with copyable financial table cards.
function renderMarkdown(md, query) {
  const lines = String(md || "").replace(/\r\n/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let tableIdx = 0;

  const inline = (text) => {
    const tokens = String(text ?? "").split(/(\*\*[^*]+\*\*|`[^`]+`)/g);
    return tokens
      .map((tok) => {
        if (tok.startsWith("**") && tok.endsWith("**") && tok.length > 4) {
          return `<strong>${highlightRaw(tok.slice(2, -2), query)}</strong>`;
        }
        if (tok.startsWith("`") && tok.endsWith("`") && tok.length > 2) {
          return `<code class="mono">${highlightRaw(tok.slice(1, -1), query)}</code>`;
        }
        return highlightRaw(tok, query);
      })
      .join("");
  };

  while (i < lines.length) {
    const line = lines[i];
    if (!line.trim()) {
      i += 1;
      continue;
    }
    // Markdown table block
    if (line.includes("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1])) {
      const headers = splitMarkdownRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) {
        rows.push(splitMarkdownRow(lines[i]));
        i += 1;
      }
      const tid = `fin-table-${tableIdx++}`;
      const thead = `<tr>${headers.map((h) => `<th>${inline(h)}</th>`).join("")}</tr>`;
      const tbody = rows
        .map((r) => `<tr>${headers.map((_, idx) => `<td>${inline(r[idx] ?? "")}</td>`).join("")}</tr>`)
        .join("");
      out.push(`
        <div class="table-card" data-table-id="${tid}">
          <div class="table-card-head">
            <span>Table · ${rows.length} rows × ${headers.length} cols</span>
            <div class="toolbar-actions">
              <button type="button" class="btn btn-xs" data-copy-table="${tid}" data-delim=",">Copy CSV</button>
              <button type="button" class="btn btn-xs" data-copy-table="${tid}" data-delim="\t">Copy Excel</button>
            </div>
          </div>
          <div class="table-scroll">
            <table class="fin-table" id="${tid}"><thead>${thead}</thead><tbody>${tbody}</tbody></table>
          </div>
        </div>
      `);
      continue;
    }
    // Headings
    const hm = /^(#{1,3})\s+(.+)$/.exec(line);
    if (hm) {
      const lvl = hm[1].length;
      out.push(`<h${lvl}>${inline(hm[2])}</h${lvl}>`);
      i += 1;
      continue;
    }
    // Bullet list
    if (/^\s*[-*]\s+/.test(line)) {
      const items = [];
      while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) {
        items.push(`<li>${inline(lines[i].replace(/^\s*[-*]\s+/, ""))}</li>`);
        i += 1;
      }
      out.push(`<ul>${items.join("")}</ul>`);
      continue;
    }
    // Paragraph
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^#{1,3}\s+/.test(lines[i]) &&
      !/^\s*[-*]\s+/.test(lines[i]) &&
      !(lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?[\s:-]+\|[\s|:-]*$/.test(lines[i + 1]))
    ) {
      para.push(lines[i]);
      i += 1;
    }
    out.push(`<p>${inline(para.join(" "))}</p>`);
  }
  return out.join("\n");
}

function tableToDelimited(tableEl, delim) {
  const rows = [...tableEl.querySelectorAll("tr")];
  return rows
    .map((tr) =>
      [...tr.querySelectorAll("th,td")]
        .map((cell) => {
          const text = cell.textContent.trim();
          if (delim === "," && (text.includes(",") || text.includes('"') || text.includes("\n"))) {
            return `"${text.replace(/"/g, '""')}"`;
          }
          return text;
        })
        .join(delim)
    )
    .join("\n");
}

function updateUrl(replace = false) {
  let url = "/";
  if (state.view === "reader" && state.docId) {
    const params = new URLSearchParams();
    if (state.activeLocator) params.set("loc", state.activeLocator);
    if (state.activeQuery) params.set("q", state.activeQuery);
    const qs = params.toString();
    url = `/d/${encodeURIComponent(state.docId)}${qs ? `?${qs}` : ""}`;
  } else if (state.view === "xbrl") {
    url = `/?view=xbrl${state.selectedCompany ? `&company=${encodeURIComponent(state.selectedCompany)}` : ""}`;
  } else if (state.selectedCompany) {
    url = `/?company=${encodeURIComponent(state.selectedCompany)}`;
  }
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
}

function syncNavTabs() {
  document.querySelectorAll(".nav-tab").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.view === state.view);
  });
  const label = document.getElementById("cmd-trigger-label");
  if (state.activeQuery) {
    label.innerHTML = `Search: <strong style="color:var(--amber)">${esc(state.activeQuery)}</strong>`;
  } else {
    label.textContent = `Search ${state.me ? state.me.total_documents.toLocaleString() : ""} filings, tickers, phrases…`;
  }
}

/* ==================== COMPANIES MATRIX VIEW ==================== */
async function selectCompany(sourceKey, { replaceUrl = true } = {}) {
  const reqId = ++state.companySeq;
  state.selectedCompany = sourceKey;
  updateUrl(replaceUrl);
  const qs = new URLSearchParams({ source_key: sourceKey, limit: "200" });
  if (state.familyFilter) qs.set("form_family", state.familyFilter);
  const data = await apiGet(`/api/documents?${qs}`);
  if (reqId !== state.companySeq) return;
  state.companyDocs = data.documents || [];
  renderCompaniesView();
}

function renderCompanyListItems(filterVal) {
  const filtered = state.companies.filter(
    (c) =>
      !filterVal ||
      c.name.toLowerCase().includes(filterVal) ||
      c.ticker.toLowerCase().includes(filterVal) ||
      c.source_key.toLowerCase().includes(filterVal)
  );
  const activeComp = state.companies.find((c) => c.source_key === state.selectedCompany) || filtered[0];
  return filtered
    .map(
      (c) => `
    <div class="company-row ${activeComp && c.source_key === activeComp.source_key ? "active" : ""}" data-select-company="${esc(c.source_key)}">
      <div>
        <div class="company-row-title">${esc(c.ticker || c.source_key)}</div>
        <div class="company-row-sub">${esc(c.name)}</div>
      </div>
      <div class="company-row-right">
        <span class="badge ${c.document_count > 0 ? "badge-blue" : "badge-muted"}">${c.document_count}</span>
        ${c.has_company_facts ? '<span class="badge badge-green">XBRL</span>' : ""}
      </div>
    </div>
  `
    )
    .join("");
}

function renderCompaniesView() {
  syncNavTabs();
  const main = document.getElementById("main-content");
  const filterVal = (document.getElementById("company-filter-input")?.value || "").toLowerCase();
  const filtered = state.companies.filter(
    (c) =>
      !filterVal ||
      c.name.toLowerCase().includes(filterVal) ||
      c.ticker.toLowerCase().includes(filterVal) ||
      c.source_key.toLowerCase().includes(filterVal)
  );
  const activeComp = state.companies.find((c) => c.source_key === state.selectedCompany) || filtered[0];

  const families = [
    ["", "All"],
    ["annual_report", "Annual"],
    ["quarterly_report", "Quarterly"],
    ["interim_report", "Interim"],
    ["results_release", "Results"],
    ["current_report", "Current"],
    ["proxy", "Proxy"],
  ];

  main.innerHTML = `
    <div class="companies-layout">
      <aside class="company-list-pane">
        <div class="pane-search">
          <input id="company-filter-input" type="search" placeholder="Filter ${state.companies.length} companies…" value="${esc(filterVal)}" />
        </div>
        <div class="company-list">
          ${filtered
            .map(
              (c) => `
            <div class="company-row ${activeComp && c.source_key === activeComp.source_key ? "active" : ""}" data-select-company="${esc(c.source_key)}">
              <div>
                <div class="company-row-title">${esc(c.ticker || c.source_key)}</div>
                <div class="company-row-sub">${esc(c.name)}</div>
              </div>
              <div class="company-row-right">
                <span class="badge ${c.document_count > 0 ? "badge-blue" : "badge-muted"}">${c.document_count}</span>
                ${c.has_company_facts ? '<span class="badge badge-green">XBRL</span>' : ""}
              </div>
            </div>
          `
            )
            .join("")}
        </div>
      </aside>
      <section class="matrix-pane">
        ${
          activeComp
            ? `
          <div class="matrix-head">
            <div>
              <h1 class="matrix-title">${esc(activeComp.name)} <span class="badge badge-blue">${esc(activeComp.ticker)}</span></h1>
              <div class="doc-sub">
                ${activeComp.document_count} filings · Sources: ${esc((activeComp.adapters || []).join(", ") || "none")}
                ${activeComp.latest_filing_date ? ` · Latest: ${esc(activeComp.latest_filing_date)}` : ""}
              </div>
            </div>
            ${
              activeComp.has_company_facts
                ? `<button type="button" class="btn btn-accent" id="open-company-xbrl">View XBRL Statements ↗</button>`
                : ""
            }
          </div>
          <div class="filter-pills">
            ${families
              .map(
                ([k, lbl]) =>
                  `<button type="button" class="pill ${state.familyFilter === k ? "active" : ""}" data-family="${k}">${lbl}</button>`
              )
              .join("")}
          </div>
          ${
            state.companyDocs.length
              ? `
            <div class="filing-table-wrap">
              <table class="filing-table">
                <thead>
                  <tr><th>Form</th><th>Filed</th><th>Period</th><th>Title</th><th>Source</th></tr>
                </thead>
                <tbody>
                  ${state.companyDocs
                    .map(
                      (d) => `
                    <tr data-open-doc="${esc(d.document_id)}">
                      <td><span class="badge badge-blue">${esc(d.display_form_type || d.form_type)}</span></td>
                      <td class="mono">${esc(d.filing_date)}</td>
                      <td class="mono">${esc(d.period_end || "—")}</td>
                      <td>${esc(d.title || d.form_type)}</td>
                      <td><span class="badge badge-muted">${esc(d.adapter)}</span></td>
                    </tr>
                  `
                    )
                    .join("")}
                </tbody>
              </table>
            </div>
          `
              : `<div class="empty-state">No filings stored for this filter yet.</div>`
          }
        `
            : `<div class="empty-state">Select a company to inspect its filing matrix.</div>`
        }
      </section>
    </div>
  `;

  const inp = document.getElementById("company-filter-input");
  const listEl = main.querySelector(".company-list");
  const bindCompanyRowClicks = () => {
    main.querySelectorAll("[data-select-company]").forEach((el) => {
      el.addEventListener("click", () => selectCompany(el.dataset.selectCompany));
    });
  };
  if (inp && listEl) {
    inp.addEventListener("input", () => {
      listEl.innerHTML = renderCompanyListItems(inp.value.toLowerCase());
      bindCompanyRowClicks();
    });
  }
  bindCompanyRowClicks();
  main.querySelectorAll("[data-family]").forEach((el) => {
    el.addEventListener("click", () => {
      state.familyFilter = el.dataset.family;
      if (state.selectedCompany) selectCompany(state.selectedCompany);
    });
  });
  main.querySelectorAll("[data-open-doc]").forEach((el) => {
    el.addEventListener("click", () => openDocument(el.dataset.openDoc));
  });
  document.getElementById("open-company-xbrl")?.addEventListener("click", () => {
    state.view = "xbrl";
    loadXbrlView();
  });
}

/* ==================== 3-PANE READER VIEW ==================== */
async function openDocument(documentId, { locator = "", query = "", replaceUrl = false } = {}) {
  const reqId = ++state.openDocSeq;
  state.view = "reader";
  state.docId = documentId;
  state.activeQuery = query || "";
  state.readerMode = "structured";
  state.diffData = null;
  syncNavTabs();

  const qs = query ? `?q=${encodeURIComponent(query)}` : "";
  const detail = await apiGet(`/api/documents/${encodeURIComponent(documentId)}${qs}`);
  if (reqId !== state.openDocSeq) return;
  state.docDetail = detail;
  state.selectedCompany = detail.document.source_key;
  state.diffTargetId =
    detail.document.prev_document_id || detail.document.peer_documents?.[0]?.document_id || null;

  let chosenLoc = locator;
  if (!chosenLoc && detail.outline.length) {
    const withHit = query ? detail.outline.find((o) => o.hit_count > 0) : null;
    chosenLoc = (withHit || detail.outline[0]).locator;
  }
  state.activeLocator = chosenLoc;
  updateUrl(replaceUrl);

  await Promise.all([loadActiveSection(), loadCompanyFacts(detail.document.source_key)]);
  if (reqId !== state.openDocSeq) return;
  if (state.drawerTab === "redline" && state.diffTargetId) {
    await loadDiff();
    if (reqId !== state.openDocSeq) return;
  }
  renderReaderView();
}

async function loadActiveSection() {
  if (!state.docId) return;
  const qs = new URLSearchParams();
  if (state.activeLocator) qs.set("locator", state.activeLocator);
  if (state.activeQuery) qs.set("q", state.activeQuery);
  state.sectionData = await apiGet(`/api/documents/${encodeURIComponent(state.docId)}/section?${qs}`);
  state.activeLocator = state.sectionData.locator || state.activeLocator;
  state.hitIndex = -1;
}

async function loadDiff() {
  if (!state.docId || !state.diffTargetId) {
    state.diffData = null;
    return;
  }
  const qs = new URLSearchParams({
    locator: state.activeLocator || "",
    changes_only: state.diffChangesOnly ? "1" : "0",
  });
  state.diffData = await apiGet(
    `/api/documents/${encodeURIComponent(state.docId)}/diff/${encodeURIComponent(state.diffTargetId)}?${qs}`
  );
}

async function loadCompanyFacts(sourceKey) {
  if (!sourceKey) return;
  state.companyFacts = await apiGet(`/api/companies/${encodeURIComponent(sourceKey)}/facts`);
}

function jumpHit(delta) {
  const hits = [...document.querySelectorAll(".md-content mark.hit")];
  if (!hits.length) return;
  hits.forEach((h) => h.classList.remove("current-hit"));
  state.hitIndex = (state.hitIndex + delta + hits.length) % hits.length;
  const target = hits[state.hitIndex];
  target.classList.add("current-hit");
  target.scrollIntoView({ behavior: "smooth", block: "center" });
}

function renderReaderView() {
  syncNavTabs();
  const main = document.getElementById("main-content");
  if (!state.docDetail) {
    main.innerHTML = `<div class="empty-state">Select a filing from Companies or press <kbd class="kbd">⌘K</kbd> to search.</div>`;
    return;
  }
  const doc = state.docDetail.document;
  const outline = state.docDetail.outline || [];
  const sec = state.sectionData;
  const isHtmlRaw = (doc.source_mime || "").toLowerCase().includes("html");
  const rawUrl = `/api/documents/${encodeURIComponent(doc.document_id)}/raw`;

  const drawerContent =
    state.drawerTab === "redline"
      ? renderRedlineDrawer(doc)
      : renderXbrlDrawer(state.companyFacts);

  main.innerHTML = `
    <div class="reader-layout">
      <aside class="pane pane-left">
        <div class="doc-meta-box">
          <div class="doc-ticker">
            <span>${esc(doc.ticker || doc.source_key)}</span>
            <span class="badge badge-blue">${esc(doc.display_form_type)}</span>
            <span class="badge badge-muted">${esc(doc.period_end || doc.filing_date)}</span>
          </div>
          <div class="doc-sub">Filed ${esc(doc.filing_date)} · ${esc(doc.adapter)} · ${outline.length} sections</div>
        </div>
        <div class="pane-search">
          <input id="in-doc-search" type="search" placeholder="Highlight in filing…" value="${esc(state.activeQuery)}" />
        </div>
        <div class="pane-header">
          <span class="pane-title">Outline${state.activeQuery ? ` · ${state.docDetail.total_hits} hits` : ""}</span>
          ${state.activeQuery ? '<span class="kbd">n / p</span>' : ""}
        </div>
        <div class="outline-list">
          ${outline
            .map(
              (o) => `
            <div class="outline-item ${o.locator === state.activeLocator ? "active" : ""}" data-select-loc="${esc(o.locator)}" title="${esc(o.title)}">
              <span class="outline-label">${esc(o.title || o.locator)}</span>
              ${o.hit_count > 0 ? `<span class="badge badge-amber">${o.hit_count}</span>` : ""}
            </div>
          `
            )
            .join("")}
        </div>
      </aside>

      <section class="pane pane-center">
        <div class="reader-toolbar">
          <div class="segmented">
            <button type="button" class="seg-btn ${state.readerMode === "structured" ? "active" : ""}" data-mode="structured">Structured</button>
            <button type="button" class="seg-btn ${state.readerMode === "raw" ? "active" : ""}" data-mode="raw" ${doc.has_raw_artifact ? "" : "disabled"}>Original (Sandboxed)</button>
          </div>
          <div class="toolbar-actions">
            <button type="button" class="btn" id="copy-citation-btn">Copy Citation</button>
            ${
              doc.prev_document_id
                ? `<button type="button" class="btn btn-accent" id="quick-diff-btn">Diff vs Prior</button>`
                : ""
            }
          </div>
        </div>
        ${
          state.readerMode === "raw"
            ? `
          <div class="raw-viewer-wrap">
            <div class="raw-notice">
              <span>Sandboxed original ${isHtmlRaw ? "HTML (scripts disabled via CSP + iframe sandbox)" : "PDF"} from immutable CAS.</span>
              <a href="${rawUrl}" target="_blank" rel="noopener noreferrer" class="btn btn-xs">Open in new tab ↗</a>
            </div>
            <iframe class="raw-frame" ${isHtmlRaw ? 'sandbox=""' : ""} src="${rawUrl}" title="Original filing"></iframe>
          </div>
        `
            : `
          <div class="reader-body">
            <h1 class="reader-h1">${esc(sec?.title || doc.title)}</h1>
            <div class="cite-bar">
              <span>${esc(sec?.citation || "")}</span>
              ${sec?.ocr_used ? '<span class="badge badge-amber">OCR</span>' : ""}
            </div>
            <div class="md-content">${renderMarkdown(sec?.markdown || "", state.activeQuery)}</div>
          </div>
        `
        }
      </section>

      <aside class="pane pane-right">
        <div class="drawer-tabs">
          <button type="button" class="drawer-tab ${state.drawerTab === "redline" ? "active" : ""}" data-drawer="redline">YoY Redline</button>
          <button type="button" class="drawer-tab ${state.drawerTab === "xbrl" ? "active" : ""}" data-drawer="xbrl">XBRL Audit</button>
        </div>
        <div class="drawer-body">${drawerContent}</div>
      </aside>
    </div>
  `;

  // Wire reader events
  main.querySelectorAll("[data-select-loc]").forEach((el) => {
    el.addEventListener("click", async () => {
      state.activeLocator = el.dataset.selectLoc;
      updateUrl(true);
      await loadActiveSection();
      if (state.drawerTab === "redline" && state.diffTargetId) await loadDiff();
      renderReaderView();
    });
  });
  main.querySelectorAll("[data-mode]").forEach((el) => {
    el.addEventListener("click", () => {
      state.readerMode = el.dataset.mode;
      renderReaderView();
    });
  });
  main.querySelectorAll("[data-drawer]").forEach((el) => {
    el.addEventListener("click", async () => {
      state.drawerTab = el.dataset.drawer;
      if (state.drawerTab === "redline" && state.diffTargetId && !state.diffData) {
        await loadDiff();
      }
      renderReaderView();
    });
  });
  document.getElementById("copy-citation-btn")?.addEventListener("click", async () => {
    const text = [sec?.citation, sec?.viewer_url || window.location.href].filter(Boolean).join(" ");
    await navigator.clipboard.writeText(text);
    showToast("Citation copied to clipboard");
  });
  document.getElementById("quick-diff-btn")?.addEventListener("click", async () => {
    state.drawerTab = "redline";
    state.diffTargetId = doc.prev_document_id;
    await loadDiff();
    renderReaderView();
  });
  main.querySelectorAll("[data-copy-table]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const tbl = document.getElementById(btn.dataset.copyTable);
      if (!tbl) return;
      const delim = btn.dataset.delim === "\t" ? "\t" : ",";
      await navigator.clipboard.writeText(tableToDelimited(tbl, delim));
      showToast(delim === "\t" ? "Table copied for Excel (TSV)" : "Table copied as CSV");
    });
  });
  const docSearch = document.getElementById("in-doc-search");
  docSearch?.addEventListener("keydown", async (e) => {
    if (e.key === "Enter") {
      await openDocument(state.docId, { locator: state.activeLocator, query: docSearch.value.trim() });
    }
  });
  document.getElementById("diff-peer-select")?.addEventListener("change", async (e) => {
    state.diffTargetId = e.target.value || null;
    await loadDiff();
    renderReaderView();
  });
  document.getElementById("diff-changes-only")?.addEventListener("change", async (e) => {
    state.diffChangesOnly = e.target.checked;
    await loadDiff();
    renderReaderView();
  });
  main.querySelectorAll("[data-jump-doc]").forEach((btn) => {
    btn.addEventListener("click", () => openDocument(btn.dataset.jumpDoc));
  });
}

function renderRedlineDrawer(doc) {
  const peers = doc.peer_documents || [];
  if (!peers.length) {
    return `<div class="empty-state">No prior ${esc(doc.form_family.replace("_", " "))} filing available to diff.</div>`;
  }
  const controls = `
    <select id="diff-peer-select" class="select-input" aria-label="Compare against prior filing">
      ${!state.diffTargetId ? '<option value="" selected>Select filing to compare…</option>' : ""}
      ${peers
        .map(
          (p) =>
            `<option value="${esc(p.document_id)}" ${p.document_id === state.diffTargetId ? "selected" : ""}>vs ${esc(p.display_form_type)} (${esc(p.filing_date)})</option>`
        )
        .join("")}
    </select>
    <div style="display:flex;align-items:center;justify-content:space-between;font-size:12px;">
      <label style="display:inline-flex;align-items:center;gap:6px;cursor:pointer;">
        <input type="checkbox" id="diff-changes-only" ${state.diffChangesOnly ? "checked" : ""} />
        Changes only
      </label>
      ${
        state.diffData
          ? `<span><span class="badge badge-green">+${state.diffData.added_words}</span> <span class="badge badge-red">-${state.diffData.removed_words}</span></span>`
          : ""
      }
    </div>
  `;
  if (!state.diffData) {
    return `${controls}<div class="empty-state">Select a prior filing to compute word-level redline.</div>`;
  }
  if (!state.diffData.hunks.length) {
    return `${controls}<div class="empty-state">No textual differences in this section.</div>`;
  }
  const hunksHtml = state.diffData.hunks
    .map((h) => {
      const inner = h.spans
        .map((sp) => {
          const txt = esc(sp.text);
          if (sp.op === "insert") return `<span class="diff-ins">${txt}</span>`;
          if (sp.op === "delete") return `<span class="diff-del">${txt}</span>`;
          return txt;
        })
        .join("");
      return `<div class="diff-card">${inner}</div>`;
    })
    .join("");
  return controls + hunksHtml;
}

function renderXbrlDrawer(facts) {
  if (!facts || !facts.has_facts || !facts.rows?.length) {
    return `<div class="empty-state">No SEC XBRL companyfacts cached for this issuer.</div>`;
  }
  const latestPeriod = facts.periods[0];
  return `
    <div class="pane-title">${esc(facts.ticker)} · ${esc(latestPeriod)} (Click to open source)</div>
    <div class="xbrl-card">
      ${facts.rows
        .map((row) => {
          const cell = row.values[latestPeriod];
          if (!cell) return "";
          const formatted = formatNumber(cell.val, cell.unit);
          const action = cell.document_id
            ? `<button type="button" class="xbrl-link" data-jump-doc="${esc(cell.document_id)}">${esc(formatted)} ↗</button>`
            : `<span class="mono">${esc(formatted)}</span>`;
          return `
            <div class="xbrl-row">
              <div>
                <div>${esc(row.label)}</div>
                <div class="xbrl-sub">${esc(cell.form)} · ${esc(cell.end)}</div>
              </div>
              <div>${action}</div>
            </div>
          `;
        })
        .join("")}
    </div>
  `;
}

/* ==================== XBRL COMPARE VIEW ==================== */
async function loadXbrlView() {
  state.view = "xbrl";
  syncNavTabs();
  const xbrlCompanies = state.companies.filter((c) => c.has_company_facts);
  if (!state.selectedCompany || !xbrlCompanies.some((c) => c.source_key === state.selectedCompany)) {
    state.selectedCompany = xbrlCompanies[0]?.source_key || null;
  }
  updateUrl(true);
  if (state.selectedCompany) {
    await loadCompanyFacts(state.selectedCompany);
  }
  renderXbrlView(xbrlCompanies);
}

function renderXbrlView(xbrlCompanies) {
  const main = document.getElementById("main-content");
  const f = state.companyFacts;
  if (!xbrlCompanies.length) {
    main.innerHTML = `<div class="empty-state">No SEC XBRL companyfacts cached yet.</div>`;
    return;
  }
  const periods = f?.periods || [];
  const rows = f?.rows || [];
  main.innerHTML = `
    <div class="xbrl-page">
      <div class="matrix-head">
        <div>
          <h1 class="matrix-title">Standardized XBRL Financials</h1>
          <div class="doc-sub">Annual US-GAAP / IFRS facts from SEC companyfacts · Click any figure to open its source filing</div>
        </div>
        <div class="toolbar-actions">
          <select id="xbrl-company-select" class="select-input" style="width:240px;">
            ${xbrlCompanies
              .map(
                (c) =>
                  `<option value="${esc(c.source_key)}" ${c.source_key === state.selectedCompany ? "selected" : ""}>${esc(c.ticker)} — ${esc(c.name)}</option>`
              )
              .join("")}
          </select>
          <button type="button" class="btn" id="xbrl-copy-csv">Copy CSV</button>
          <button type="button" class="btn" id="xbrl-copy-tsv">Copy Excel</button>
        </div>
      </div>
      ${
        rows.length
          ? `
        <div class="table-card">
          <div class="table-scroll">
            <table class="fin-table" id="xbrl-main-table">
              <thead>
                <tr><th>Metric</th>${periods.map((p) => `<th>${esc(p)}</th>`).join("")}</tr>
              </thead>
              <tbody>
                ${rows
                  .map(
                    (r) => `
                  <tr>
                    <td><strong>${esc(r.label)}</strong><div class="xbrl-sub">${esc(r.concept || "")}</div></td>
                    ${periods
                      .map((p) => {
                        const cell = r.values[p];
                        if (!cell) return `<td>—</td>`;
                        const txt = formatNumber(cell.val, cell.unit);
                        return cell.document_id
                          ? `<td><button type="button" class="xbrl-link" data-jump-doc="${esc(cell.document_id)}">${esc(txt)} ↗</button></td>`
                          : `<td>${esc(txt)}</td>`;
                      })
                      .join("")}
                  </tr>
                `
                  )
                  .join("")}
              </tbody>
            </table>
          </div>
        </div>
      `
          : `<div class="empty-state">No standardized annual facts found for this issuer.</div>`
      }
    </div>
  `;

  document.getElementById("xbrl-company-select")?.addEventListener("change", async (e) => {
    state.selectedCompany = e.target.value;
    await loadXbrlView();
  });
  document.getElementById("xbrl-copy-csv")?.addEventListener("click", async () => {
    const tbl = document.getElementById("xbrl-main-table");
    if (tbl) {
      await navigator.clipboard.writeText(tableToDelimited(tbl, ","));
      showToast("XBRL table copied as CSV");
    }
  });
  document.getElementById("xbrl-copy-tsv")?.addEventListener("click", async () => {
    const tbl = document.getElementById("xbrl-main-table");
    if (tbl) {
      await navigator.clipboard.writeText(tableToDelimited(tbl, "\t"));
      showToast("XBRL table copied for Excel");
    }
  });
  main.querySelectorAll("[data-jump-doc]").forEach((btn) => {
    btn.addEventListener("click", () => openDocument(btn.dataset.jumpDoc));
  });
}

/* ==================== GLOBAL CMD+K PALETTE ==================== */
function openCmdPalette() {
  state.cmdOpen = true;
  document.getElementById("cmd-backdrop").classList.remove("hidden");
  const inp = document.getElementById("cmd-input");
  inp.value = state.activeQuery || "";
  inp.focus();
  inp.select();
  runCmdSearch();
}

function closeCmdPalette() {
  state.cmdOpen = false;
  document.getElementById("cmd-backdrop").classList.add("hidden");
}

async function runCmdSearch() {
  const reqId = ++state.cmdSearchSeq;
  const q = document.getElementById("cmd-input").value.trim();
  const compFilter = document.getElementById("cmd-company-filter").value;
  const famFilter = document.getElementById("cmd-family-filter").value;
  const resultsEl = document.getElementById("cmd-results");

  if (!q) {
    state.cmdItems = state.companies.slice(0, 15).map((c) => ({
      kind: "company",
      source_key: c.source_key,
      title: `${c.ticker || c.source_key} — ${c.name}`,
      meta: `${c.document_count} filings`,
    }));
    state.cmdSelected = 0;
    renderCmdResults();
    return;
  }

  resultsEl.innerHTML = `<div class="loading-state" style="padding:18px;">Searching filings…</div>`;
  const qs = new URLSearchParams({ q, limit: "25" });
  if (compFilter) qs.set("source_key", compFilter);
  if (famFilter) qs.set("form_family", famFilter);
  try {
    const data = await apiGet(`/api/search?${qs}`);
    if (reqId !== state.cmdSearchSeq) return;
    const compMatches = state.companies
      .filter(
        (c) =>
          c.name.toLowerCase().includes(q.toLowerCase()) ||
          c.ticker.toLowerCase().includes(q.toLowerCase())
      )
      .slice(0, 3)
      .map((c) => ({
        kind: "company",
        source_key: c.source_key,
        title: `${c.ticker || c.source_key} — ${c.name}`,
        meta: `Jump to company · ${c.document_count} filings`,
      }));
    const docMatches = (data.results || []).map((r) => ({
      kind: "hit",
      document_id: r.document_id,
      locator: r.locator,
      query: q,
      source_key: r.source_key,
      form: r.display_form_type || r.form_type,
      date: r.filing_date,
      snippet: r.snippet,
    }));
    state.cmdItems = [...compMatches, ...docMatches];
    state.cmdSelected = 0;
    renderCmdResults();
  } catch (err) {
    resultsEl.innerHTML = `<div class="empty-state">Search error: ${esc(err.message)}</div>`;
  }
}

function renderCmdResults() {
  const el = document.getElementById("cmd-results");
  if (!state.cmdItems.length) {
    el.innerHTML = `<div class="empty-state" style="padding:20px;">No matching filings or companies.</div>`;
    return;
  }
  el.innerHTML = state.cmdItems
    .map((item, idx) => {
      const sel = idx === state.cmdSelected ? "selected" : "";
      if (item.kind === "company") {
        return `
          <div class="cmd-item ${sel}" data-cmd-idx="${idx}">
            <div class="cmd-item-head"><span class="badge badge-blue">COMPANY</span> ${esc(item.title)}</div>
            <div class="cmd-item-snippet">${esc(item.meta)}</div>
          </div>
        `;
      }
      return `
        <div class="cmd-item ${sel}" data-cmd-idx="${idx}">
          <div class="cmd-item-head">
            <span class="badge badge-amber">${esc(item.source_key)}</span>
            <span class="badge badge-blue">${esc(item.form)}</span>
            <span class="mono" style="font-size:11px;color:var(--text-muted)">${esc(item.date)} · ${esc(item.locator)}</span>
          </div>
          <div class="cmd-item-snippet">${highlightRaw(item.snippet, item.query)}</div>
        </div>
      `;
    })
    .join("");

  el.querySelectorAll("[data-cmd-idx]").forEach((row) => {
    row.addEventListener("click", () => activateCmdItem(Number(row.dataset.cmdIdx)));
  });
}

async function activateCmdItem(idx) {
  const item = state.cmdItems[idx];
  if (!item) return;
  closeCmdPalette();
  if (item.kind === "company") {
    state.view = "companies";
    await selectCompany(item.source_key);
  } else {
    await openDocument(item.document_id, { locator: item.locator, query: item.query });
  }
}

/* ==================== BOOTSTRAP ==================== */
async function routeFromLocation({ replaceUrl = true } = {}) {
  const pathMatch = /^\/d\/([^/]+)$/.exec(window.location.pathname);
  const params = new URLSearchParams(window.location.search);
  if (pathMatch) {
    await openDocument(decodeURIComponent(pathMatch[1]), {
      locator: params.get("loc") || "",
      query: params.get("q") || "",
      replaceUrl,
    });
  } else if (params.get("view") === "xbrl") {
    state.selectedCompany = params.get("company") || null;
    await loadXbrlView();
  } else {
    state.view = "companies";
    const initialComp = params.get("company") || state.companies[0]?.source_key;
    if (initialComp) await selectCompany(initialComp, { replaceUrl });
    else renderCompaniesView();
  }
}

async function init() {
  try {
    const [me, compData] = await Promise.all([apiGet("/api/me"), apiGet("/api/companies")]);
    state.me = me;
    state.companies = compData.companies || [];

    document.getElementById("coverage-badge").textContent = `${me.covered_companies}/${me.company_count} live`;
    document.getElementById("user-label").textContent = me.person_id || me.name;
    if (me.has_logout) {
      document.getElementById("logout-form").classList.remove("hidden");
    }

    const compSel = document.getElementById("cmd-company-filter");
    state.companies.forEach((c) => {
      const opt = document.createElement("option");
      opt.value = c.source_key;
      opt.textContent = `${c.ticker || c.source_key} — ${c.name}`;
      compSel.appendChild(opt);
    });

    await routeFromLocation({ replaceUrl: true });
  } catch (err) {
    document.getElementById("main-content").innerHTML = `<div class="empty-state">Failed to load workspace: ${esc(err.message)}</div>`;
  }
}

window.addEventListener("popstate", () => {
  routeFromLocation({ replaceUrl: true }).catch(() => {});
});

// Global listeners
document.getElementById("cmd-trigger").addEventListener("click", openCmdPalette);
document.getElementById("cmd-backdrop").addEventListener("click", (e) => {
  if (e.target.id === "cmd-backdrop") closeCmdPalette();
});
document.getElementById("cmd-input").addEventListener("input", () => {
  clearTimeout(state.cmdTimer);
  state.cmdTimer = setTimeout(runCmdSearch, 140);
});
document.getElementById("cmd-company-filter").addEventListener("change", runCmdSearch);
document.getElementById("cmd-family-filter").addEventListener("change", runCmdSearch);

document.querySelectorAll(".nav-tab").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const target = btn.dataset.view;
    if (target === "companies") {
      state.view = "companies";
      if (state.selectedCompany) await selectCompany(state.selectedCompany);
      else renderCompaniesView();
    } else if (target === "reader") {
      state.view = "reader";
      renderReaderView();
    } else if (target === "xbrl") {
      await loadXbrlView();
    }
  });
});

document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (state.cmdOpen) closeCmdPalette();
    else openCmdPalette();
    return;
  }
  if (state.cmdOpen) {
    if (e.key === "Escape") {
      closeCmdPalette();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      state.cmdSelected = Math.min(state.cmdSelected + 1, Math.max(0, state.cmdItems.length - 1));
      renderCmdResults();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      state.cmdSelected = Math.max(state.cmdSelected - 1, 0);
      renderCmdResults();
    } else if (e.key === "Enter") {
      e.preventDefault();
      activateCmdItem(state.cmdSelected);
    }
    return;
  }
  const tag = (document.activeElement?.tagName || "").toLowerCase();
  if (tag === "input" || tag === "textarea" || tag === "select") return;
  if (e.key === "/") {
    e.preventDefault();
    openCmdPalette();
  } else if (state.view === "reader" && e.key === "n") {
    jumpHit(1);
  } else if (state.view === "reader" && e.key === "p") {
    jumpHit(-1);
  }
});

init();
