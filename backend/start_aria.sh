#!/usr/bin/env bash
# ARIA start script — expects real keys in the environment (or backend/.env)
# No secrets are hardcoded here. Copy backend/.env.example and fill it in.

set -a
# shellcheck disable=SC1091
[ -f .env ] && . ./.env
set +a

: "${HTTP_HOST:=127.0.0.1}"
: "${HTTP_PORT:=8765}"
: "${DEEPSEEK_API_KEY:?set DEEPSEEK_API_KEY in backend/.env}"
: "${GEMINI_API_KEYS:?set GEMINI_API_KEYS in backend/.env}"
: "${GROQ_API_KEYS:?set GROQ_API_KEYS in backend/.env}"

exec .venv/Scripts/python.exe -m uvicorn aria.main:app --host "$HTTP_HOST" --port "$HTTP_PORT"
