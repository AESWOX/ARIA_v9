# ARIA — Manifest (auto-generated)

> **Дата**: 2026-08-19
> **Вердикт**: `DOD_READY`
> **Elapsed**: ?s
> Источник: `dod_verify.py --json`

## Проверки DoD

- ✅ **database**: ok
- ✅ **models**: ok (18 tables)
- ✅ **router**: ok (13 providers, 5 classes)
- ✅ **config**: ok
- ✅ **provider_catalog**: 280 models cached
- ✅ **vault**: ok
- ✅ **skills**: 73 skills in skills_meta
- ✅ **budget**: warn@80% block@100%
- ✅ **guardrails**: ok (3 detectors via before_call/after_call: exact-failure, same-tool, no-progress)
- ✅ **vault**: 179 .md files
- ✅ **Backend tests**: 152/152 PASSED
- ✅ **Frontend TSC**: OK
- ✅ **Frontend build**: OK (2234 modules)
- ✅ **Frontend install**: OK
- ✅ **Hermes refs in src/**: 0 files

## База данных

- **provider_models**: 280
- **skills_meta**: 73
- **Real providers**: deepseek-chat, deepseek-reasoner, gemini-flash, gemini-pro, gemini-pro-premium-fallback, gemini-vision, groq-llama-fast, groq-llama-versatile, groq-llama-versatile-premium-fallback, groq-subagent-fast
- **Stale providers**: none

## Архитектурные решения

- **state.db (1.3GB Hermes legacy)**: A — archived as read-only
  ARIA has its own schema (15 tables), Hermes state schema is different.
  Migration would not provide value proportional to effort.
- **Database profile**: A — dev-only (SQLite)
  Local budget agent, no production deployment target.
  Not production-ready for multi-user / HA scenarios.
- **Frontend @nous-research/ui**: React 19 upgrade (from 18)
  Peer dependency satisfied, npm ci now works without --legacy-peer-deps.

<!-- GENERATED_BY=generate_manifest.py -->
<!-- DOD_TIMESTAMP=2026-08-19T18:58:01 -->
<!-- DOD_VERDICT=DOD_READY -->