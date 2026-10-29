// Amberstone enquiry assistant — chat UI logic.
// Kept in an external file (not inline) so the page can enforce a strict CSP
// (script-src 'self'), which hardens the innerHTML render path below.

const msgs = document.getElementById("chat-msgs");
const input = document.getElementById("chat-text");
const sendBtn = document.getElementById("chat-send");
// This visit's session: the server issues it (POST /api/session, opened on
// page load) and reuses a live one for the same network for 24h. Chat sends it
// as X-Session-Id and the server keeps the conversation's context under it.
let sessionPromise = null;
// The CRM page (crm.js) shows a visitor only the leads from their own chat:
// it sends the session id stored here. Both pages are on one origin, so they
// share localStorage. If storage is blocked, the CRM just shows no leads.
const SESSION_KEY = "amberstone.chat_session_id";
function rememberSession(id) {
  try { localStorage.setItem(SESSION_KEY, id); } catch { /* storage blocked */ }
}

// Where the visitor came from, for the admin's referrer_tag: a ?ref= tag on
// the page link, else the referring site's host name.
function referrerTag() {
  try {
    const ref = new URLSearchParams(location.search).get("ref");
    if (ref) return ref.slice(0, 100);
    if (document.referrer) return new URL(document.referrer).hostname.slice(0, 100) || null;
  } catch { /* malformed referrer */ }
  return null;
}

async function openSession() {
  const tag = referrerTag();
  const query = tag ? `?referrer_tag=${encodeURIComponent(tag)}` : "";
  const res = await fetch(`/api/session${query}`, { method: "POST" });
  if (!res.ok) throw new Error(`session ${res.status}`);
  const data = await res.json();
  rememberSession(data.session_id);
  if (data.max_input_chars) input.maxLength = data.max_input_chars;
  return data.session_id;
}

// The open session, opening one if needed (a failed open is retried later).
function ensureSession() {
  if (!sessionPromise) {
    sessionPromise = openSession().catch((e) => { sessionPromise = null; throw e; });
  }
  return sessionPromise;
}

async function postChat(text) {
  const sid = await ensureSession();
  return fetch("/api/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Session-Id": sid },
    body: JSON.stringify({ message: text }),
  });
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
    let res = await postChat(text);
    if (res.status === 404) {
      // The session expired (24h) or is gone: open a new one and retry once.
      sessionPromise = null;
      res = await postChat(text);
    }
    typing.remove();
    if (!res.ok) {
      addMsg("Sorry, something went wrong. Please try again.", "bot");
    } else {
      const data = await res.json();
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
ensureSession().catch(() => { /* opened again on the first message */ });
