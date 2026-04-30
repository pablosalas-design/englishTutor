# Workspace

## Overview

pnpm workspace monorepo using TypeScript. Each package manages its own dependencies.

## Stack

- **Monorepo tool**: pnpm workspaces
- **Node.js version**: 24
- **Package manager**: pnpm
- **TypeScript version**: 5.9
- **API framework**: Express 5
- **Database**: PostgreSQL + Drizzle ORM
- **Validation**: Zod (`zod/v4`), `drizzle-zod`
- **API codegen**: Orval (from OpenAPI spec)
- **Build**: esbuild (CJS bundle)

## Key Commands

- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- `pnpm --filter @workspace/api-server run dev` — run API server locally

See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details.

## Telegram English Tutor Bot

`bot.py` is a Telegram bot acting as an English tutor, powered by GPT-4o. Deployed on Railway from the GitHub repo `pablosalas-design/englishTutor`.

### Users / personas

- **Peace** (voice `coral`, adult tone) — used by **Pablo** (the project owner). B1→B2 level.
- **Mia para Lucía** (voice `sage`, kid tone) — daughter Lucía, A2→B1.
- **Mia para Leyre** (voice `sage`, kid tone) — daughter Leyre, A2→B1.

There is no "wife" persona. Do not assume one.

### Companion web app (`webapp.py` + `static/`)

FastAPI app served on port 5000. PWA installable. Voice tutoring via OpenAI Realtime API (WebRTC). Shares the Postgres DB with the Telegram bot.

Flow: persona picker → activity picker (`Hablar` / `Gramática`) → voice screen or grammar screen.

#### Grammar feature

- `GET /api/grammar/today?mode={peace|lucia|leyre}` returns today's lesson (cached per UTC day per `chat_id`). If none exists, calls GPT-4o (JSON mode) using:
  - persona level (`peace`=B2-C1 / kids=A2-B1) and explanation language (`peace`=en / kids=es)
  - the user's last 60 messages from the shared `messages` table (to detect weak areas)
  - the **per-level curriculum** in `LEVEL_CURRICULUM` (`B2-C1` and `A2-B1` lists in `webapp.py`) — the model is forced to pick one of those topic slugs. Editable in one place without touching anything else.
  - the last 20 lesson topics for that profile (to avoid repetition)
- Returns: `{topic, title, explanation, examples[{en,translation}], exercises[mc..., fill...]}`. The generator validates the JSON shape against the per-profile plan and retries once if invalid.
- Per-profile exercise plan (`EXERCISE_PLAN_BY_MODE` in `webapp.py`):
  - `peace` → 10 exercises (6 mc + 4 fill)
  - `lucia`, `leyre` → 5 exercises (3 mc + 2 fill)
- `POST /api/grammar/attempt` re-evaluates correctness on the server (does not trust the client's verdict), checks that the lesson belongs to the caller's profile, and records the attempt in `grammar_attempts`.
- `POST /api/grammar/regenerate` regenerates ONLY the exercises for an existing lesson (same topic/explanation, fresh content). Validates lesson ownership, replaces `exercises` in DB, deletes old `grammar_attempts` for that `lesson_id`. Driven by `REGEN_EXERCISES_SYSTEM_PROMPT` and `regenerate_exercises_for_lesson()`. Used by the result screen's "Repetir con ejercicios nuevos" button.
- DB tables: `grammar_lessons` (UNIQUE `chat_id`+`lesson_date`) and `grammar_attempts`.
- `web_chat_id` mapping: `peace=-1001`, `lucia=-1002`, `leyre=-1003`.

#### Vocabulary feature (phrasal verbs + Leitner SRS)

- `GET /api/vocab/today?mode={peace|lucia|leyre}` returns today's vocab session: `{level, mode, study, reviews_count, exercises, totals}`.
  - `study`: NEW phrasal verbs the user has never seen, capped at the per-mode plan (peace=5/day, kids=3/day).
  - `exercises`: meaning-MC quiz built from the new ones + due reviews (max reviews per mode: peace=10, kids=6). Distractors are random meanings from other phrasal verbs at the same level.
- `POST /api/vocab/answer?mode=...` body `{phrasal_id, user_answer}` re-evaluates correctness on the server (compares against `phrasal_verbs.meaning_es`) and updates the Leitner box for that `(chat_id, phrasal_id)` pair. Intervals (days): box1=1, box2=3, box3=7, box4=14, box5=30. Correct → box+1 (max 5). Wrong → box=1.
- `MODE_TO_VOCAB_LEVEL`: `peace`→`B2-C1`, `lucia`/`leyre`→`A2-B1`.
- Pool seeding is **lazy and per-level**:
  - On first `/api/vocab/today` for an unseen level, generates a `VOCAB_SEED_BATCH` (=40) using `gpt-4o-mini` with structured JSON output. ~30s, costs cents.
  - When the user has fewer than `VOCAB_REFILL_THRESHOLD` (=8) unseen items left for their level, generates another `VOCAB_REFILL_BATCH` (=20). Existing phrasals are sent in the prompt's exclude list to avoid duplicates.
  - Generation is idempotent thanks to `UNIQUE(level, phrasal)`.
- DB tables: `phrasal_verbs` (level + phrasal verb + Spanish meaning + English definition + 2 examples) and `phrasal_progress` (per chat_id: box, times_seen, times_correct, last_seen_at, next_due_at).

#### Static assets cache

Query string `?v=14` on `app.js` and `styles.css`; service worker cache is `tutor-shell-v11`. After deploying, do a hard refresh (or close/reopen the PWA) so the new SW activates.

The voice screen uses only the animated orb (`#orb`). The previous 3D avatar system (Ready Player Me / `.glb` model, three.js, `avatar.js`, `AVATAR_*` env vars) was fully removed.

### Pending improvements (roadmap)

1. **Conversation memory** — let the bot remember recent messages so it can give more coherent corrections and contextual replies.
2. **`/reset` command** — clear the memory and start a fresh conversation.
3. **Difficulty levels** — commands like `/beginner`, `/intermediate`, `/advanced` to adjust the language complexity.
4. **"Correction-only" vs "conversation" modes** — toggle between just fixing a sentence or chatting back.
5. **"Typing…" indicator** — show a typing action in Telegram while GPT is generating the reply.
6. **Friendlier error handling** — if OpenAI fails or times out, send a graceful message instead of going silent.
7. **Voice message support** — accept audio in English, transcribe it, correct it, and optionally reply with voice.
