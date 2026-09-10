# Lead-to-Lease: Real Estate Enquiry Assistant (backend)

A conversational lead-intake assistant for a property agency. Instead of a
static enquiry form, a visitor chats in plain English; the assistant qualifies
the enquiry (budget, area, property type, timeline, financing, intent), scores
it, saves the lead, and fires a day-0 confirmation email. A lightweight CRM
board lets the agency work the resulting pipeline.

This is the API service, and it is self-contained: FastAPI serves both the
visitor site and the chat endpoint from one origin, persists everything to
Postgres, and drives the conversation with Claude through the Claude Agent SDK.
The demo runs on fictional branding ("Amberstone Realty") and captures only the
data a visitor volunteers.

## How it works

```
visitor message --> /api/chat --> per-IP daily rate check (Postgres, atomic)
                                        |
                                        v
                      Claude Agent SDK (system prompt, in-process MCP server)
                        - qualifies the enquiry across the conversation
                        - calls the save_lead tool once name + email are known
                                        |
                                        v
        save_lead: deterministic rule score (0-100) + LLM grade/rationale
                        --> INSERT lead + day-0 confirmation email (soft-fail)
                                        |
                                        v
                     reply streamed back; every turn logged to Postgres
```

The bot's only way to touch data is a single agent tool, `save_lead`. The Agent
SDK is configured so the model cannot reach for anything else: the built-in
tool set is disabled (`tools=[]`), only the in-process lease MCP tools are
allow-listed, `permission_mode="dontAsk"` denies everything unapproved without
prompting, and `setting_sources=[]` isolates the run from any local filesystem
settings or `CLAUDE.md`. Multi-turn context is carried by passing the prior
`session_id` back as the SDK's `resume`.

Lead scoring is split on purpose: Python rules turn the extracted fields into a
reproducible 0-100 number (budget stated, timeline, financing, intent), while
the LLM adds a `hot`/`warm`/`cold` grade with a one-line rationale. The two are
stored side by side so the human number and the model's read can be compared.

## Security

The model is treated as untrusted, and the enquiry endpoint is public. The
defenses that matter here:

1. **A single, typed agent tool.** The bot can only call `save_lead`; all other
   tools (including the SDK's built-in Bash/Read/Write) are hard-disabled, and
   `permission_mode="dontAsk"` denies anything unapproved without hanging.
2. **Validation at every boundary.** HTTP requests are Pydantic-validated
   (message length capped, `session_id` length-bound because it is
   client-controlled and fed to the SDK's `resume`); the `save_lead` tool
   re-validates and normalizes every field before it is persisted.
3. **Per-IP abuse cap.** A Postgres-backed daily counter (atomic
   `INSERT ... ON CONFLICT` then `SELECT ... FOR UPDATE`) rate-limits chat per
   client IP and stays correct across multiple workers. `X-Forwarded-For` is
   trusted only when `trust_proxy` is set.
4. **Fail-open where safe, soft-fail where cosmetic.** A transient rate-limit
   table error lets the request through rather than taking down the demo; the
   day-0 email and per-turn logging are best-effort and never break a reply.
5. **Defense-in-depth headers.** A strict Content-Security-Policy (scripts stay
   same-origin), `X-Content-Type-Options`, `Referrer-Policy`, `X-Frame-Options`,
   and HSTS are set on every response.

See [SECURITY.md](SECURITY.md) for the full threat model, the accepted
demo-only risks, and what to change before pointing this at real data.

## Stack

- **Python 3.12+ / FastAPI** (async), **Pydantic** for validation at every boundary
- **Postgres** via **SQLAlchemy 2.0 async** + the **asyncpg** driver
- **LLM**: Claude via the **Claude Agent SDK** (`claude-agent-sdk`), with an
  in-process MCP server exposing the single `save_lead` tool
- **Email**: stdlib `smtplib` over SSL (app-password auth), run off the event
  loop with `asyncio.to_thread`
- Static visitor site + CRM board served by the same app (no separate frontend
  server, so the browser calls `/api/chat` same-origin with no CORS)

## API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET   | `/` | Visitor site (chat lives in the right-hand panel) |
| GET   | `/health` | Liveness probe |
| POST  | `/api/chat` | Visitor message in, assistant reply + `session_id` out |
| GET   | `/crm` | CRM-lite opportunities board (business-facing) |
| GET   | `/api/crm/leads` | All captured leads, scored, newest/highest first |
| PATCH | `/api/crm/leads/{id}/stage` | Move a lead between pipeline stages |

The CRM surface is intentionally unauthenticated in this demo (see SECURITY.md).

## Running it locally

Prerequisites: Python 3.12+ and Postgres running locally. For the Claude Agent
SDK you need a working Claude Code login on the machine (or
`CLAUDE_CODE_OAUTH_TOKEN` set). No API key is used.

1. Create the database (tables are created automatically on first startup):
   ```bash
   createdb lead_to_lease
   ```
2. Create a virtualenv and install dependencies:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   ```
3. Configure the environment:
   ```bash
   cp .env.example .env
   # set DATABASE_URL; leave ZOHO_EMAIL empty to skip real email in dev
   ```
4. Run the API (serves the site and the chat endpoint on :8091):
   ```bash
   ./run.sh
   ```
5. Open http://localhost:8091 and chat, then open http://localhost:8091/crm to
   see the captured lead on the pipeline board.

## Project structure

```
real-estate-backend/
├── run.sh                Start uvicorn on :8091
├── requirements.txt      Pinned dependencies
├── app/
│   ├── main.py           FastAPI app: static site, /api/chat, CRM routes, headers
│   ├── config.py         Pydantic settings (env-driven)
│   ├── db.py             Async engine + session factory + declarative Base
│   ├── bootstrap.py      Create tables on startup
│   ├── models.py         SQLAlchemy models: Lead, EmailLog, ChatMessage, IpUsage
│   ├── schemas.py        HTTP request/response contracts + pipeline stages
│   ├── scoring.py        Deterministic 0-100 lead score
│   ├── ratelimit.py      Postgres-backed per-IP daily cap (atomic)
│   ├── chatlog.py        Persist each chat turn
│   ├── emailer.py        Day-0 confirmation email (SMTP over SSL, soft-fail)
│   ├── agent/            Claude Agent SDK: client, prompts, tool registry, save_lead
│   └── static/           Visitor site (lead.html + app.js) and CRM (crm.html + crm.js)
└── .env.example          Copy to .env and fill in
```

## License

MIT. See [LICENSE](LICENSE).
