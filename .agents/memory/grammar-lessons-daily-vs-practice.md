---
name: grammar_lessons daily vs practice rows
description: How daily and "Practicar este tema" lessons stay separate in the grammar_lessons table without overwriting each other
---

# grammar_lessons: daily vs practice rows

`grammar_lessons` (webapp.py) holds two kinds of rows distinguished by `is_practice`:
- daily auto lesson (`is_practice=FALSE`) — at most one per `(chat_id, lesson_date)`
- on-demand practice lessons (`is_practice=TRUE`) — one per `(chat_id, lesson_date, topic)`

## The rule
These two row types must NEVER collide or overwrite each other, even when they share the same topic on the same day. This is enforced by **two partial unique indexes**, not one combined index:
- `uq_grammar_lessons_daily` ON `(chat_id, lesson_date) WHERE is_practice = FALSE`
- `uq_grammar_lessons_practice` ON `(chat_id, lesson_date, topic) WHERE is_practice = TRUE`

`insert_lesson()` picks the matching `ON CONFLICT (...) WHERE is_practice = ...` target based on its `is_practice` arg.

**Why:** A single non-partial `UNIQUE(chat_id, lesson_date, topic)` lets a daily and a practice lesson with the same topic collide — the upsert overwrites one with the other's content/`is_practice`, which either corrupts the daily lesson or makes `fetch_today_lesson` (filters `is_practice=FALSE`) never find it, causing infinite regeneration.

**How to apply:** Any new write path into `grammar_lessons`, or any change to its uniqueness, must keep the two row types on separate partial indexes and pick the correct `ON CONFLICT` target. Don't merge them back into one index.
