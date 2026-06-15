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
- For `fill` exercises the model is instructed (in both `GRAMMAR_SYSTEM_PROMPT` and `REGEN_EXERCISES_SYSTEM_PROMPT`) to list **every interchangeable correct answer** in `accept[]` (e.g. *provided that / providing / as long as*), so valid synonyms aren't marked wrong. The server (`evaluate_answer`) checks `correct` + `accept` after `normalize_fill_text`. (`unless` ≠ `provided that`/`as long as` — only true synonyms are grouped.)
- Per-profile exercise plan (`EXERCISE_PLAN_BY_MODE` in `webapp.py`):
  - `peace` → 10 exercises (6 mc + 4 fill)
  - `lucia`, `leyre` → 5 exercises (3 mc + 2 fill)
- `POST /api/grammar/attempt` re-evaluates correctness on the server (does not trust the client's verdict), checks that the lesson belongs to the caller's profile, and records the attempt in `grammar_attempts`.
- `POST /api/grammar/regenerate` regenerates ONLY the exercises for an existing lesson (same topic/explanation, fresh content). Validates lesson ownership, replaces `exercises` in DB, deletes old `grammar_attempts` for that `lesson_id`. Driven by `REGEN_EXERCISES_SYSTEM_PROMPT` and `regenerate_exercises_for_lesson()`. Used by the result screen's "Repetir con ejercicios nuevos" button.
- DB tables: `grammar_lessons` (has `is_practice` boolean; two partial unique indexes keep daily and practice rows separate — `uq_grammar_lessons_daily` on `(chat_id,lesson_date) WHERE is_practice=FALSE` and `uq_grammar_lessons_practice` on `(chat_id,lesson_date,topic) WHERE is_practice=TRUE`; `insert_lesson` targets the matching one) and `grammar_attempts`.
- `web_chat_id` mapping: `peace=-1001`, `lucia=-1002`, `leyre=-1003`.

#### Progress map + on-demand practice

- Activity card "📊 Mi progreso" (`actProgress`) in the subpicker opens a `#progress` screen (a "semáforo" map of every curriculum topic).
- `GET /api/grammar/progress?mode={peace|lucia|leyre}` returns `{level, mode, summary{total,mastered,in_progress,not_started}, topics[{slug,label,status,attempts,accuracy}]}`. One row per topic in that level's `LEVEL_CURRICULUM`. Status: `mastered` (≥6 attempts AND ≥80% accuracy), `in_progress` (any attempts but not mastered), `not_started` (no attempts). Spanish labels come from `TOPIC_LABELS` / `grammar_topic_label`; computed by `build_grammar_progress`.
- `POST /api/grammar/practice?mode=...` body `{topic}` creates an extra **practice** lesson for that topic (`is_practice=TRUE`, via `create_practice_lesson` → `generate_lesson(forced_topic=)`). Practice lessons are excluded from `fetch_today_lesson` (which filters `is_practice=FALSE`) so the daily lesson is unaffected. The frontend reuses the existing grammar screen/flow (intro → exercises → result, incl. attempts + regenerate) for practice lessons.
- Frontend: `startProgress` / `renderProgress` / `practiceTopic` in `app.js`; `.prog-*` styles in `styles.css`.

#### Vocabulary feature (phrasal verbs + Leitner SRS)

- `GET /api/vocab/today?mode={peace|lucia|leyre}` returns today's vocab session: `{level, mode, study, reviews_count, exercises, totals}`.
  - `study`: NEW phrasal verbs the user has never seen, capped at the per-mode plan (peace=5/day, kids=3/day).
  - `exercises`: quiz built from the new ones + due reviews (max reviews per mode: peace=10, kids=6). Two exercise types are mixed: `meaning_mc` (choose the Spanish meaning) and `phrasal_write` (writing: a cloze English sentence + Spanish hint, user types the phrasal verb). Distractors are random meanings from other phrasal verbs at the same level.
  - Exercise count: peace targets `target_exercises`=15 per session (in `VOCAB_PLAN_BY_MODE`); when there aren't enough unique items, `build_vocab_exercises` builds both an MC and a write variant per phrasal and repeats to reach the target. Kids have no target → 1 exercise per item (new + due reviews).
  - `phrasal_write` answers are checked server-side with `normalize_phrasal_text` (lowercase, strip punctuation, collapse spaces) against `phrasal_verbs.phrasal`. The cloze is built by `make_cloze_for_phrasal` (handles common verb forms: -s/-ed/-ing/-ies); if no form is found in any example, that phrasal falls back to MC.
- `POST /api/vocab/answer?mode=...` body `{phrasal_id, user_answer, exercise_type}` re-evaluates correctness on the server (for `meaning_mc` compares against `phrasal_verbs.meaning_es`; for `phrasal_write` normalizes and compares against `phrasal_verbs.phrasal`) and updates the Leitner box for that `(chat_id, phrasal_id)` pair. Intervals (days): box1=1, box2=3, box3=7, box4=14, box5=30. Correct → box+1 (max 5). Wrong → box=1.
- `MODE_TO_VOCAB_LEVEL`: `peace`→`B2-C1`, `lucia`/`leyre`→`A2-B1`.
- Pool seeding is **lazy and per-level**:
  - On first `/api/vocab/today` for an unseen level, generates a `VOCAB_SEED_BATCH` (=40) using `gpt-4o-mini` with structured JSON output. ~30s, costs cents.
  - When the user has fewer than `VOCAB_REFILL_THRESHOLD` (=8) unseen items left for their level, generates another `VOCAB_REFILL_BATCH` (=20). Existing phrasals are sent in the prompt's exclude list to avoid duplicates.
  - Generation is idempotent thanks to `UNIQUE(level, phrasal)`.
- DB tables: `phrasal_verbs` (level + phrasal verb + Spanish meaning + English definition + 2 examples) and `phrasal_progress` (per chat_id: box, times_seen, times_correct, last_seen_at, next_due_at).

#### "Mis palabras" feature (personal vocab fed from Telegram)

Pablo's own class vocabulary. Words are added via the Telegram bot and reviewed in the webapp with the same Leitner SRS as Vocabulario.

- **Feeding (bot.py):** command `/add` (registered in `main()`; documented in `/start`). Reads the raw `update.message.text` (so it captures multiple lines, not just `context.args`), strips the `/add`/`@bot` token, splits by newlines AND commas, dedupes case-insensitively (via `normalize_word`, mirrors `normalize_phrasal_text`), caps at `ADD_MAX_WORDS`=300. Enriches with `gpt-4o-mini` (JSON mode, level-aware via `MODE_TO_VOCAB_LEVEL`) in **batches** of `ENRICH_BATCH_SIZE`=20 (`enrich_words` chunks → `_enrich_words_batch` per chunk; per-batch failures are logged and skipped so one bad batch doesn't lose the rest). The blocking enrichment runs in `asyncio.to_thread` and large lists get a heads-up reply; the final summary reports added / already-had / `failed` (couldn't prepare). Output shape per word → `{display, meaning_es, definition_en, examples[{en,es}]}`. Inserts with `ON CONFLICT (chat_id, word) DO NOTHING`; replies a Spanish summary (added vs already-had). Stores under the active persona's `web_chat_id` (`WEB_CHAT_IDS` in bot.py = `peace:-1001, lucia:-1002, leyre:-1003`, must match webapp's `web_chat_id`) read from the chat's current `mode` — this is the bot↔webapp bridge that makes the words show up in the app tab.
- **Review (webapp.py):**
  - `GET /api/mywords/today?mode=` → `{mode, study[], reviews_count, exercises[], totals{new,reviews,exercises,all}}`. `study`: unseen words (`times_seen=0`), capped at the per-mode vocab plan's `new`. `reviews`: due words with **`times_seen>0`** (unseen words are never pulled in as reviews — they must be studied first). Exercises mix `meaning_mc` (choose the Spanish meaning; distractors from the user's OTHER words, fallback `GENERIC_DISTRACTORS`) and `word_write` (Spanish hint → type the English word; an optional cloze from an example is shown when `make_cloze_for_phrasal` finds one, otherwise just the hint). Peace targets 15 exercises (reuses `vocab_plan_for`/`VOCAB_PLAN_BY_MODE`).
  - `POST /api/mywords/answer?mode=` body `{word_id, user_answer, exercise_type}` re-evaluates server-side (checks the word belongs to the caller's profile; for `word_write` normalizes vs `user_words.word`, for `meaning_mc` compares vs `meaning_es`) and updates the inline Leitner box. Same intervals as vocab (1/3/7/14/30 days).
  - `GET /api/mywords/list?mode=` → `{totals{total,due,new}, words[]}`; used for the subpicker card subtitle count.
- DB table `user_words` (single table, inline Leitner): `id, chat_id, word` (normalized, `UNIQUE(chat_id, word)`)`, display, meaning_es, definition_en, examples jsonb, box, times_seen, times_correct, source, created_at, first_seen_at, last_seen_at, next_due_at`, index on `(chat_id, next_due_at)`. Created idempotently in BOTH `webapp.init_db_vocab()` and `bot.init_db()`.
- Frontend: subpicker card `actMyWords`, `#mywords` screen, `startMyWords`/`renderMyWords*` in app.js (mirror the vocab flow), `refreshMyWordsCount` updates the card subtitle from `/api/mywords/list` (called from `showScreen("subpicker")`). Reuses the vocab/grammar CSS classes.

#### Static assets cache

Query string `?v=20` on `app.js` and `styles.css`; service worker cache is `tutor-shell-v17`. After deploying, do a hard refresh (or close/reopen the PWA) so the new SW activates. Bump both whenever frontend assets change.

The voice screen uses only the animated orb (`#orb`). The previous 3D avatar system (Ready Player Me / `.glb` model, three.js, `avatar.js`, `AVATAR_*` env vars) was fully removed.

### Pending improvements (roadmap)

1. **Conversation memory** — let the bot remember recent messages so it can give more coherent corrections and contextual replies.
2. **`/reset` command** — clear the memory and start a fresh conversation.
3. **Difficulty levels** — commands like `/beginner`, `/intermediate`, `/advanced` to adjust the language complexity.
4. **"Correction-only" vs "conversation" modes** — toggle between just fixing a sentence or chatting back.
5. **"Typing…" indicator** — show a typing action in Telegram while GPT is generating the reply.
6. **Friendlier error handling** — if OpenAI fails or times out, send a graceful message instead of going silent.
7. **Voice message support** — accept audio in English, transcribe it, correct it, and optionally reply with voice.
