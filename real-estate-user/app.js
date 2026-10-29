// Amberstone enquiry assistant — chat UI logic.
// Kept in an external file (not inline) so the page can enforce a strict CSP
// (script-src 'self'), which hardens the innerHTML render path below.

// ---- Config -------------------------------------------------------------
// This page is served statically (GitHub Pages) and calls the hosted API
// cross-origin. API_KEY must match the backend's API_KEY in .env. It is the
// low-value bot-gate key (not a real secret); for a public deploy, proxy the
// call instead of shipping any key to the page.
const BASE_URL = "https://demo-api.bytcra.com/realestate";  // hosted backend
const API_KEY = "deababf23bc76803b8c0c73acf33db16b90236c5339799bd7e89eddb46cd8aba";  // Karan swaps the real key at deploy
// -------------------------------------------------------------------------

const msgs = document.getElementById("chat-msgs");
const input = document.getElementById("chat-text");
const sendBtn = document.getElementById("chat-send");
let sessionId = null;  // carries multi-turn context across requests
// The CRM page (crm.js) shows a visitor only the leads from their own chat:
// it sends the session id stored here. Both pages are on one origin, so they
// share localStorage. If storage is blocked, the CRM just shows no leads.
const SESSION_KEY = "amberstone.chat_session_id";
function rememberSession(id) {
  try { localStorage.setItem(SESSION_KEY, id); } catch { /* storage blocked */ }
}

// Minimal, injection-safe Markdown: escape HTML first, then a small subset.
function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function renderMarkdown(text) {
  let s = escapeHtml(text);
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");  // **bold**
  s = s.replace(/(^|\n)\s*[-*]\s+/g, "$1• ");                // "- item" / "* item" -> "• item"
  s = s.replace(/\*([^*\n]+)\*/g, "<em>$1</em>");            // *italic*
  return s;                                                   // newlines kept; .msg has white-space: pre-wrap
}

function addMsg(text, who) {
  const el = document.createElement("div");
  el.className = "msg " + who;
  el.innerHTML = renderMarkdown(text);
  msgs.appendChild(el);
  msgs.scrollTop = msgs.scrollHeight;
  return el;
}

function setBusy(busy) {
  input.disabled = busy;
  sendBtn.disabled = busy;
  if (!busy) input.focus();
}

async function send() {
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg(text, "user");
  setBusy(true);
  const typing = addMsg("Assistant is typing…", "bot typing");
  try {
    const res = await fetch(`${BASE_URL}/api/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-API-Key": API_KEY },
      body: JSON.stringify({ message: text, session_id: sessionId }),
    });
    typing.remove();
    if (!res.ok) {
      addMsg("Sorry, something went wrong. Please try again.", "bot");
    } else {
      const data = await res.json();
      if (data.session_id) {
        sessionId = data.session_id;
        rememberSession(sessionId);
      }
      addMsg(data.reply, "bot");
    }
  } catch (e) {
    typing.remove();
    addMsg("Sorry, I couldn't reach the server. Please try again.", "bot");
  } finally {
    setBusy(false);
  }
}

sendBtn.addEventListener("click", send);
input.addEventListener("keydown", (e) => { if (e.key === "Enter") send(); });
input.focus();
