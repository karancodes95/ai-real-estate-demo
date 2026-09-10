#!/usr/bin/env bash
# Start the Amberstone Realty enquiry assistant on :8091.
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/uvicorn app.main:app --reload --port 8091
