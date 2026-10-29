// Amberstone CRM-lite, GHL-style opportunities board over /api/crm/leads.
// Stage headers show count + summed pipeline value (parsed from budget text).
// Drag a card between columns to move its stage (PATCH per drop).

const STAGES = [
  { key: "new", label: "New Lead", color: "#1e6ff5" },
  { key: "contacted", label: "Contacted", color: "#7a5af8" },
  { key: "visit", label: "Visit Scheduled", color: "#b0742c" },
  { key: "closed_won", label: "Closed Won", color: "#12805c" },
  { key: "closed_lost", label: "Closed Lost", color: "#d92d20" },
];
const board = document.getElementById("board");
const searchBox = document.getElementById("search");
const sortSel = document.getElementById("sort");
let leads = [];

// This visitor's chat session id, stored by the chat page (app.js) on the same
// origin. The API lists and moves only that session's leads; no id, no leads.
const SESSION_KEY = "amberstone.chat_session_id";
function sessionHeaders() {
  let id = null;
  try { id = localStorage.getItem(SESSION_KEY); } catch { /* storage blocked */ }
  return id ? { "X-Session-Id": id } : {};
}

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

// "80L" -> 8000000, "1.2Cr" -> 12000000, "40k" -> 40000, else 0 (unparsed).
function parseValue(budget) {
  const m = String(budget).replace(/,/g, "").match(/([\d.]+)\s*(cr|crore|l|lakh|lac|k)?/i);
  if (!m || !m[1]) return 0;
  const n = parseFloat(m[1]);
  const unit = (m[2] || "").toLowerCase();
  if (unit.startsWith("cr")) return n * 10000000;
  if (unit === "l" || unit.startsWith("lakh") || unit.startsWith("lac")) return n * 100000;
  if (unit === "k") return n * 1000;
  return n;
}

function fmtValue(v) {
  if (v >= 10000000) return "Rs " + (v / 10000000).toFixed(v % 10000000 ? 2 : 0) + "Cr";
  if (v >= 100000) return "Rs " + (v / 100000).toFixed(v % 100000 ? 1 : 0) + "L";
  if (v > 0) return "Rs " + Math.round(v).toLocaleString("en-IN");
  return "";
}

function initials(name) {
  return name.split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0].toUpperCase()).join("") || "?";
}

function gradePill(l) {
  if (!l.grade) return "";
  const cls = l.grade === "hot" ? "hot" : l.grade === "warm" ? "warm" : "";
  return `<span class="pill ${cls}">${esc(l.grade)} ${l.score}</span>`;
}

const ICONS = {
  chat: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 11a8 8 0 1 1 4.6 7.2L4 20l1.1-3.6A8 8 0 0 1 4 11z"/></svg>',
  task: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 11l3 3 8-8"/><path d="M20 12v6a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9"/></svg>',
  note: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M7 3h7l4 4v14H7z"/><path d="M14 3v4h4"/></svg>',
  tag: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12V3h9l9 9-9 9z"/><circle cx="7.5" cy="7.5" r="1.5"/></svg>',
  phone: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5c0 8 7 15 15 15l2-4-4-2-2 2a12 12 0 0 1-7-7l2-2-2-4z"/></svg>',
  cal: '<svg viewBox="0 0 24 24" fill="none" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="16" rx="2"/><path d="M8 3v4M16 3v4M4 11h16"/></svg>',
};

function cardHtml(l) {
  const what = [l.property_type, l.location].filter(Boolean).join(" in ");
  const rows = [];
  if (what) rows.push(`<div class="field"><span class="k">Looking for</span><span class="v">${esc(what)}</span></div>`);
  if (l.timeline_months) rows.push(`<div class="field"><span class="k">Timeline</span><span class="v">${l.timeline_months} months</span></div>`);
  if (l.financing !== "unknown") rows.push(`<div class="field"><span class="k">Financing</span><span class="v">${esc(l.financing.replace("_", " "))}</span></div>`);
  if (l.intent !== "unknown") rows.push(`<div class="field"><span class="k">Intent</span><span class="v">${esc(l.intent)}</span></div>`);
  rows.push(`<div class="field"><span class="k">Lead score</span><span class="v">${gradePill(l)}</span></div>`);
  const value = fmtValue(parseValue(l.budget));
  return `
    <div class="card" draggable="true" data-id="${l.id}" title="${esc(l.rationale)}">
      <div class="head">
        <span class="av">${esc(initials(l.name))}</span>
        <span class="who">
          <div class="name">${esc(l.name)}</div>
          <div class="sub">${esc(l.email)}${l.phone ? " | " + esc(l.phone) : ""}</div>
        </span>
        <span class="value">${value}</span>
      </div>
      ${rows.join("")}
      <div class="actions">
        ${ICONS.chat}${ICONS.task}${ICONS.note}${ICONS.tag}${ICONS.phone}${ICONS.cal}
        <span class="when">${esc(l.created_at)}</span>
      </div>
    </div>`;
}

function visibleLeads() {
  const q = searchBox.value.trim().toLowerCase();
  let list = leads;
  if (q) {
    list = list.filter((l) =>
      [l.name, l.email, l.phone, l.budget, l.location, l.property_type]
        .join(" ").toLowerCase().includes(q)
    );
  }
  list = [...list];
  if (sortSel.value === "recent") list.sort((a, b) => b.id - a.id);
  else list.sort((a, b) => b.score - a.score || b.id - a.id);
  return list;
}

function render() {
  const list = visibleLeads();
  board.innerHTML = STAGES.map((s) => {
    const items = list.filter((l) => l.status === s.key);
    const total = items.reduce((sum, l) => sum + parseValue(l.budget), 0);
    const totalTxt = total ? fmtValue(total) + " | " : "";
    return `
      <div class="col" data-stage="${s.key}">
        <div class="colhead">
          <div class="name"><span class="swatch" style="background:${s.color}"></span>${s.label}</div>
          <div class="totals">${totalTxt}${items.length} opportunit${items.length === 1 ? "y" : "ies"}</div>
        </div>
        <div class="cards">${items.map(cardHtml).join("")}</div>
      </div>`;
  }).join("");
  wire();
}

function wire() {
  document.querySelectorAll(".card").forEach((el) => {
    el.addEventListener("dragstart", (e) => {
      el.classList.add("dragging");
      e.dataTransfer.setData("text/plain", el.dataset.id);
      e.dataTransfer.effectAllowed = "move";
    });
    el.addEventListener("dragend", () => el.classList.remove("dragging"));
  });
  document.querySelectorAll(".col").forEach((col) => {
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("dragover"); });
    col.addEventListener("dragleave", () => col.classList.remove("dragover"));
    col.addEventListener("drop", async (e) => {
      e.preventDefault();
      col.classList.remove("dragover");
      const id = Number(e.dataTransfer.getData("text/plain"));
      const stage = col.dataset.stage;
      const lead = leads.find((l) => l.id === id);
      if (!lead || lead.status === stage) return;
      const prev = lead.status;
      lead.status = stage;
      render();
      try {
        const res = await fetch(`/api/crm/leads/${id}/stage`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json", ...sessionHeaders() },
          body: JSON.stringify({ status: stage }),
        });
        if (!res.ok) throw new Error(res.status);
      } catch {
        lead.status = prev;  // revert on failure
        render();
      }
    });
  });
}

async function load() {
  const res = await fetch("/api/crm/leads", { headers: sessionHeaders() });
  leads = res.ok ? await res.json() : [];
  render();
}

searchBox.addEventListener("input", render);
sortSel.addEventListener("change", render);
document.getElementById("refresh").addEventListener("click", load);
document.getElementById("add").addEventListener("click", () =>
  window.location.assign("/"));  // new opportunities come from the intake chat

load();
setInterval(load, 30000);  // pick up new chat leads automatically
