# Security

This is a portfolio demo that runs on fictional agency branding and captures
only the data a visitor volunteers into the chat. The threat model treats the
LLM as untrusted and the enquiry endpoint as public: the model may attempt any
action, and the system is designed so it can still only capture a lead.

## Defenses in place

1. **A single, typed agent tool** (`app/agent/client.py`, `app/agent/tools.py`):
   the bot's only capability is `save_lead`. All built-in Agent SDK tools are
   disabled (`tools=[]`), only the in-process lease MCP tools are allow-listed
   (`allowed_tools`), `permission_mode="dontAsk"` denies anything unapproved
   without prompting or hanging, and `setting_sources=[]` isolates the run from
   any local filesystem settings / `CLAUDE.md`.
2. **Validation at every boundary** (`app/schemas.py`, `app/agent/tools.py`):
   the chat request caps message length (2000 chars) and bounds `session_id`
   length (it is client-controlled and passed to the SDK's `resume`). The
   `save_lead` tool re-validates and normalizes every field, requires a name and
   a syntactically valid email before saving, and truncates free-text fields
   (rationale) before they reach the database. The lead's session id is not
   an input: it comes from the server-side turn context.
3. **Per-IP abuse cap** (`app/ratelimit.py`): a Postgres-backed daily counter,
   made race-safe with an atomic `INSERT ... ON CONFLICT DO NOTHING` followed by
   `SELECT ... FOR UPDATE`, so concurrent requests (and multiple uvicorn
   workers) cannot slip past the cap. Blocked attempts are still recorded, so
   repeat offenders stay queryable.
4. **Fail-open / soft-fail, deliberately** (`app/main.py`, `app/agent/tools.py`):
   a transient rate-limit-table error lets the request through rather than
   taking the demo down; the day-0 confirmation email and per-turn chat logging
   are best-effort and never break the reply. Every email attempt (sent /
   failed / skipped) is recorded in `email_log`.
5. **Response headers** (`app/main.py`): a strict Content-Security-Policy keeps
   scripts same-origin (the chat JS is served from `/static`; only Google Fonts
   are allowed), plus `X-Content-Type-Options: nosniff`, `Referrer-Policy:
   no-referrer`, `X-Frame-Options: DENY`, and HSTS on every response.
6. **Admin API behind its own login** (`app/admin_auth.py`, `app/admin.py`):
   the `/admin` routes (metrics, sessions with transcripts and IPs, leads)
   need a bearer JWT from `POST /admin/login`: an argon2id-hashed password
   (`ADMIN_PASSWORD_HASH`, empty = login off), a 7-day HS256 token signed
   with `ADMIN_JWT_SECRET`, and 5 failed logins per IP per 15 min. The
   public `API_KEY` never opens them. A failed chat turn stores only the
   exception's class name, never its message.
7. **The CRM shows each visitor only their own leads** (`app/main.py`,
   `app/agent/client.py`, `app/agent/tools.py`): the server picks a new
   chat's session id (a UUID passed to the SDK) and `save_lead` stores the
   turn's id on the lead, never one from the LLM or the request JSON.
   `GET /api/crm/leads` and the stage `PATCH` act only on leads whose
   session id matches the caller's `X-Session-Id` (a canonical UUID); none,
   or any other value, gets `[]` or a 404. The chat page stores its session
   id in localStorage and the CRM page, on the same origin, sends it. The
   public `API_KEY` is only a bot gate here; every lead across sessions is
   on `/admin/leads`, behind the admin JWT.

## Accepted risks (this demo)

These are acceptable because the data is fictional and self-service. They are
called out so anyone reusing this code for real data knows to change them:

1. **A chat session id is a bearer secret.** The CRM has no login: whoever
   holds a session id sees and moves that session's leads, and can resume
   that chat (`resume` takes the id the client sends). Ids are random UUIDs
   kept in the visitor's browser, so this is not guessable, but a leaked id
   (shared device, a script on the same origin) exposes that one session.
   Bind sessions to the browser that started them, or put the CRM behind a
   real login, before real leads flow through it.
2. **Lead text is stored verbatim.** Chat transcripts and lead fields are
   persisted as entered (`chat_messages`, `leads`) for demo follow-up, including
   whatever email/phone a visitor types. Add a PII scrubber / retention policy
   before real use.
3. **The chat reply is a single non-streamed response.** Fine for a demo; for
   production consider streaming and a hard per-request timeout around the SDK
   call.
4. **No CAPTCHA on the enquiry.** Abuse is bounded only by the per-IP daily cap;
   for a public production deploy add a challenge (e.g. Turnstile) on top.

## Deployment requirements

1. **Run behind a trusted reverse proxy and set `TRUST_PROXY=true`.** The app
   trusts `X-Forwarded-For` only when `trust_proxy` is set, and then reads the
   rightmost entry (the IP the proxy itself observed); the left entries are
   client-supplied and spoofable. With `TRUST_PROXY` off, it uses the socket
   peer. Never expose uvicorn directly to the internet with `TRUST_PROXY=true`,
   or the per-IP cap can be bypassed by forging the header.
2. **Keep secrets in `.env`, never committed.** Set `DATABASE_URL` and, if
   sending real email, `ZOHO_EMAIL` + `ZOHO_APP_PASSWORD` (an app-specific
   password, never your account password). Leave the email creds empty to
   disable sending. For the admin API set `ADMIN_PASSWORD_HASH` (from
   `tool/hash_admin_password.py`) and a random `ADMIN_JWT_SECRET`.
3. **Connect Postgres as a least-privileged role** scoped to the
   `lead_to_lease` database; the app only needs DML on its own tables, not
   superuser.
4. **Provide Claude Agent SDK auth out of band.** On a server, set
   `CLAUDE_CODE_OAUTH_TOKEN`; there is no API key in this codebase. Restrict who
   can read that environment.
5. **Add a request body-size limit** at the proxy (e.g. nginx
   `client_max_body_size`) as defense-in-depth on top of the app-level message
   length cap.

## Dependencies

Dependencies are hash-locked in `requirements.txt` (runtime) and
`requirements-dev.txt` (tests), from `requirements.in` and
`requirements-dev.in`. Install with `--require-hashes --only-binary=:all:`.
Each version is 14+ days old and was checked against OSV; the lock headers
list the exceptions and the known advisories still open. Run `pip-audit`
against the lock in CI as a standing gate and keep the Agent SDK and
transport libraries current.

## Reporting

Found something? Open a GitHub issue (no sensitive details) or reach out via the
contact link in the project README.
