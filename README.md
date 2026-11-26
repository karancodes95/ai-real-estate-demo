# Lead-to-Lease: AI Real Estate Pipeline Demo

A conversational intake chat that replaces the enquiry form for a fictional property agency, Amberstone Realty. The assistant qualifies the enquiry (budget, area, property type, timeline, financing, intent), scores the lead, saves it and sends a day-0 confirmation email. Fictional data only.

**Live demo:** https://demo.bytcra.com/real-estate/

## What is in this repo

| Path | What it is |
| ---- | ---------- |
| `index.html` | Landing page: Lead Assistant, and the CRM (coming soon) |
| `real-estate-user/` | Lead Assistant: the chat page (plain HTML, CSS and JavaScript) |
| `real-estate-backend/` | The API behind the chat (FastAPI, Postgres, Claude Agent SDK). Run locally, it also serves its own copy of the site at `/` and a CRM board at `/crm` |

## How it works

```
page open --> session from the API (one per visitor IP, 24h)
                                    |
                                    v
visitor message --> per-IP daily limit --> Claude Agent SDK qualifies the enquiry
                                    |
                                    v
     save_lead tool: 0-100 rule score + LLM hot/warm/cold grade
                                    |
                                    v
          lead saved + day-0 email --> reply back to the chat
```

The bot can touch data through one tool only, `save_lead`. All other agent tools are disabled. Scoring is split on purpose: Python rules give a reproducible 0-100 number, and the LLM adds a hot, warm or cold grade with a one-line reason, stored side by side.

## Security

The model is treated as untrusted and the chat endpoint is public. One typed agent tool, validation at every boundary, a per-IP daily cap, and strict security headers including a Content-Security-Policy. Details: [real-estate-backend/README.md](real-estate-backend/README.md) and [real-estate-backend/SECURITY.md](real-estate-backend/SECURITY.md).

## Stack

- **Front end:** plain HTML, CSS and JavaScript, no build step; scripts load from the page's own origin only (`script-src 'self'`)
- **Backend:** Python 3.12+, FastAPI, Pydantic, Postgres via SQLAlchemy 2.0 async and asyncpg
- **LLM:** Claude via the Claude Agent SDK, with an in-process MCP server exposing `save_lead`
- **Email:** standard-library SMTP over SSL

## Hosting

1. **Pages:** static files served by nginx on an Oracle Cloud (OCI) Ubuntu server, behind Cloudflare, at https://demo.bytcra.com/real-estate/. GitHub Pages also publishes them from this repo's `main` branch.
2. **API:** the backend runs under uvicorn as a systemd service bound to localhost on the same Oracle Cloud server, behind nginx and Cloudflare, at `https://demo-api.bytcra.com/realestate`. Its Postgres database runs on the same server.

## Deployment

1. **Pages on demo.bytcra.com:** changed files are copied to the server's web root over SSH, then their SHA-256 checksums are compared with the repo copies.
2. **GitHub Pages:** each push to `main` runs GitHub's `pages-build-deployment` workflow, which republishes the pages.
3. **API:** the server runs the backend from a git checkout of this repo. To deploy, pull `main` there, install any changed requirements into the backend's virtualenv (see [real-estate-backend/README.md](real-estate-backend/README.md)), and restart the backend's systemd service.
4. **Settings:** a `.env` file next to the backend on the server. Git ignores it, so it stays out of the repo.

## Run it locally

1. Follow [real-estate-backend/README.md](real-estate-backend/README.md). It serves the site and the chat on `http://localhost:8091`, and the CRM board on `http://localhost:8091/crm`.
2. The standalone page in `real-estate-user/` calls the hosted API at `https://demo-api.bytcra.com/realestate`. To point it at a local API, change `BASE_URL` and `API_KEY` in `real-estate-user/app.js`.

## License

MIT. See [real-estate-backend/LICENSE](real-estate-backend/LICENSE).

---

Built by [Bytcra](https://bytcra.com)
