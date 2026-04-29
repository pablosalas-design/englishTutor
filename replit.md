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

### Pending improvements (roadmap)

1. **Conversation memory** — let the bot remember recent messages so it can give more coherent corrections and contextual replies.
2. **`/reset` command** — clear the memory and start a fresh conversation.
3. **Difficulty levels** — commands like `/beginner`, `/intermediate`, `/advanced` to adjust the language complexity.
4. **"Correction-only" vs "conversation" modes** — toggle between just fixing a sentence or chatting back.
5. **"Typing…" indicator** — show a typing action in Telegram while GPT is generating the reply.
6. **Friendlier error handling** — if OpenAI fails or times out, send a graceful message instead of going silent.
7. **Voice message support** — accept audio in English, transcribe it, correct it, and optionally reply with voice.
