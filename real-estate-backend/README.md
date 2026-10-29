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
| GET   | `/api/crm/leads` | The caller's own leads (`X-Session-Id`), scored, highest first |
| PATCH | `/api/crm/leads/{id}/stage` | Move one of the caller's own leads between stages |
| POST  | `/admin/login` | `{password}` in, `{token, expires_at}` out (7-day admin JWT) |
| POST  | `/admin/logout` | No-op (the token is stateless) |
| GET   | `/admin/metrics?scope=today\|all` | Dashboard KPIs (same fields as the clinic demo; `bounce_rate_today` is null) |
| GET   | `/admin/sessions?limit&since` | Chat sessions (UUID ids only), newest activity first |
| GET   | `/admin/sessions/{uuid}` | One session's transcript, error turns included |
| GET   | `/admin/leads?limit&since` | Leads in the CRM's shape, best score first, ISO times |

The `/admin` routes need `Authorization: Bearer <token>` from `/admin/login`
(never the API key). They are read by bytcra-admin. Fields this demo does not
measure come back as null: `bounce_rate_today`, `referrer_tag`, `country`,
`expires_at`, and the `stage` of non-error turns.

The CRM routes show each visitor only the leads from their own chat: they
need `X-Session-Id`, the chat's session id (the chat page stores it in
localStorage and the CRM page sends it). No session, or a non-UUID one, gets
`[]` or a 404. The lead's session id is set server-side by `save_lead`.
Every lead across sessions is only on `/admin/leads` (see SECURITY.md).

## Running it locally

Prerequisites: Python 3.14 (the locks also install on 3.12) and Postgres running locally. For the Claude Agent
SDK you need a working Claude Code login on the machine (or
`CLAUDE_CODE_OAUTH_TOKEN` set). No API key is used.

1. Create the database (tables are created automatically on first startup):
   ```bash
   createdb lead_to_lease
   ```
2. Create a virtualenv (Python 3.14) and install the hash-locked
   dependencies (wheels only; add `-r requirements-dev.txt` for the tests):
   ```bash
   python3.14 -m venv .venv
   .venv/bin/pip install --require-hashes --only-binary=:all: \
     -r requirements.txt -r requirements-dev.txt
   ```
   To change a dependency, edit `requirements.in` / `requirements-dev.in` and
   re-lock (see their headers).
3. Configure the environment:
   ```bash
   cp .env.example .env
   # set DATABASE_URL and API_KEY; leave ZOHO_EMAIL empty to skip real email in dev
   # admin API: ADMIN_PASSWORD_HASH from the helper below, a random ADMIN_JWT_SECRET
   .venv/bin/python tool/hash_admin_password.py
   ```
4. Run the API (serves the site and the chat endpoint on :8091):
   ```bash
   ./run.sh
   ```
5. Open http://localhost:8091 and chat, then open http://localhost:8091/crm in
   the same browser to see the captured lead on the pipeline board (it shows
   the leads of that browser's latest chat only).
6. Run the tests (a throwaway `lead_to_lease_test` database on the local
   Postgres, created and dropped by the run; the Agent SDK is mocked):
   ```bash
   .venv/bin/pytest -q
   ```

## Project structure

```
real-estate-backend/
├── run.sh                Start uvicorn on :8091
├── requirements.in       Runtime inputs (top-level pins)
├── requirements.txt      Hash-locked runtime dependencies
├── requirements-dev.in   Test inputs
├── requirements-dev.txt  Hash-locked test dependencies
├── pytest.ini            Test config (asyncio)
├── tool/
│   └── hash_admin_password.py  argon2id hash of the admin password for .env
├── tests/                Admin API, login, error logging (ASGITransport, test DB)
├── app/
│   ├── main.py           FastAPI app: static site, /api/chat, CRM routes, headers
│   ├── admin_auth.py     POST /admin/login (argon2id + JWT), the bearer gate
│   ├── admin.py          /admin metrics, sessions and leads for bytcra-admin
│   ├── deps.py           API-key gate and client IP
│   ├── config.py         Pydantic settings (env-driven)
│   ├── db.py             Async engine + session factory + declarative Base
│   ├── bootstrap.py      Create tables on startup
│   ├── models.py         SQLAlchemy models: Lead, EmailLog, ChatMessage, IpUsage
│   ├── schemas.py        HTTP request/response contracts + pipeline stages
│   ├── scoring.py        Deterministic 0-100 lead score
│   ├── ratelimit.py      Postgres-backed per-IP daily cap (atomic)
│   ├── chatlog.py        Persist each chat turn (plus an error row on failure)
│   ├── emailer.py        Day-0 confirmation email (SMTP over SSL, soft-fail)
│   ├── agent/            Claude Agent SDK: client, prompts, tool registry, save_lead
│   └── static/           Visitor site (lead.html + app.js) and CRM (crm.html + crm.js)
└── .env.example          Copy to .env and fill in
```

## License

MIT. See [LICENSE](LICENSE).
