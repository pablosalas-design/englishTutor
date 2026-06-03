---
name: Inline-Leitner "unseen looks due" gotcha
description: Single-table SRS where the progress row is created up front needs a times_seen>0 gate on review queries.
---

# Inline-Leitner: unseen items look "due"

When a Leitner/SRS feature stores progress columns **inline on the same row as the item**
(e.g. `user_words` for "Mis palabras": box/times_seen/next_due_at live on the word row,
created at insert time with `next_due_at = NOW()`), every brand-new item is immediately
"due" by `next_due_at <= NOW()`.

**Rule:** review queries must gate on `times_seen > 0` (or equivalent "has been studied"),
otherwise unseen words get pulled into the review/exercise set before ever appearing in the
study cards — breaking the new-vs-review SRS flow.

**Why:** the webapp Vocabulario feature does NOT hit this because its progress lives in a
SEPARATE table (`phrasal_progress`) whose row only exists after the first answer; a missing
row = not due. The inline single-table design trades that natural guard for simplicity, so the
guard must be added explicitly.

**How to apply:** any new single-table SRS (progress columns on the item row) → filter reviews
by `times_seen > 0 AND next_due_at <= NOW()`; keep "new/study" as `times_seen = 0`.
