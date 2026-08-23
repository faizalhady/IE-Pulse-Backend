# Universe Chat — design (2026-08-23)

> **Built 2026-08-23.** Backend on `universe/phase-4` (`modules/universe/chat/`, `api/routers/universe_chat.py`, 13 tests), frontend on `universe/ask` (`src/components/ask/`, `src/pages/ask/`, 5 tests). Verified in the browser: a first question streams tool cards and the answer, the URL adopts the thread, a reopened chat renders as it streamed, thumbs-down stores its reason, the drawer opens on the Cycle Time page. Two things learned: never remount the chat while a stream runs (the URL change and the thread fetch must not swap the component), and AI Elements' registry no longer has `response`/`actions`/`loader` — `MessageResponse` lives in `message`, thumbs and the spinner are ours.

The chatbot client for the Jabil Universe: a page and a drawer inside IE-Pulse, streaming
answers from the engine that already exists (universe views → three tools → model chain →
glossary). Decided with Faiz in the brainstorm of 2026-08-23.

## Decisions

| Decision | Choice |
|---|---|
| Where it lives | Inside IE-Pulse: an **Ask** page (`/ietools/ask/`) plus a floating bubble + right drawer on every module page (the only idea kept from chat v1) |
| UI stack | Vercel AI SDK (`ai`, `@ai-sdk/react` `useChat`) + AI Elements (shadcn components) on the existing React 18 / Vite / Tailwind / Radix stack |
| Transport | FastAPI streams the **AI SDK UI message stream v1** over SSE — text deltas, tool calls, tool results, finish — so `useChat` and AI Elements work unmodified |
| Answer look | **No bubble for the answer**: it flows full-width like a document. Only the user's question sits in a bubble. Tool steps ("ran SQL → 40 rows") fold under the answer |
| Answer shape | **No template.** Readable, no padding; the shape follows the question (a number → the number and a table; "draft an email" → paragraphs). SQL and tool steps never in the prose |
| Saved chats | Yes, per user: list / reopen / rename / delete. Continuing a chat re-sends its messages, trimmed to the token budget |
| Feedback | Thumbs up / down on every answer; down opens an optional reason box; stored on the message |
| Access | A pilot list of NTIDs (`UNIVERSE_CHAT_USERS`); everyone else 403. The existing IE-Pulse login supplies the NTID |
| Models | The free-model chain as built (`eval/chain.py`). Employee names never reach a model (`v_employee` hidden) |
| Out of scope now | sharing a chat, search across chats, file upload, deployment to 02 (local dev only until Faiz decides) |

## Shape

```
browser ─ AskPage / AskDrawer ─ useChat(DefaultChatTransport api=/ietools/ask/api/universe/chat, body={thread_id})
                │ SSE: start · text-start/delta/end · tool-input-available · tool-output-available · finish · [DONE]
FastAPI ─ api/routers/universe_chat.py
                │ POST /api/universe/chat            run the loop, stream, save both messages
                │ GET  /api/universe/threads         list mine
                │ GET  /api/universe/threads/{id}    messages as UIMessage[] (exact reload)
                │ PATCH/DELETE /api/universe/threads/{id}
                │ POST /api/universe/feedback        {message_id, vote: 1|-1, reason?}
modules/universe/chat/
                │ loop.py     ONE loop: messages → events (the exam's answer() becomes a consumer of it)
                │ stream.py   events → AI SDK SSE lines; header x-vercel-ai-ui-message-stream: v1
                │ threads.py  SQLite chat_thread · chat_message (parts JSON, feedback) in data/operational.db
engine (unchanged) ─ chain.chat · tools.describe/query/define · Metric Glossary
```

## Backend

- **loop.py** — `run(messages, *, max_rounds=8) -> Iterator[event]`. Lifted from `eval/run.py::answer()`: same SYSTEM prompt (plus the answer principles below), same tools, same `_trim`, same forced final round, same unrun-SQL nudge. Events: `("text", delta)`, `("tool_call", id, name, args)`, `("tool_result", id, output)`, `("done", meta)` where meta = model trace, usage, sqls. `eval/run.py::answer()` calls `run()` and folds events into the record it returns today — one loop, two consumers, the exam keeps passing.
- **Streaming from the chain** — the chain's `chat()` is one-shot (no token streaming): a round's text arrives whole and is emitted as one delta. Token-level streaming is deferred; the tool cards already make the wait visible.
- **stream.py** — `sse(events) -> Iterator[str]`: `start` → per round `tool-input-available` / `tool-output-available` → `text-start/delta/end` → `finish` → `[DONE]`. Errors become `{"type":"error","errorText":…}` then finish. Runs the loop in a thread and yields from a queue, the way `cycle_time.py`'s SSE already does.
- **threads.py** — tables created in `core/database.py::init_db` (user-entered data lives in SQLite by rule):
  - `chat_thread(id TEXT PK, ntid TEXT, title TEXT, created_at, updated_at)`
  - `chat_message(id TEXT PK, thread_id, role, parts JSON, model TEXT, created_at, feedback INTEGER, feedback_reason TEXT)`
  - Title = the first question, 60 chars. Parts are stored as the AI SDK `UIMessage.parts` so a reopened chat renders exactly as it streamed.
- **Router** — `verified_ntid` on every route; `UNIVERSE_CHAT_USERS` (comma list) gates it; a thread is only visible to its owner. `POST /chat` body is what `useChat` sends (`{id, messages, trigger, ...}` plus `thread_id`); the last user message is the new question; history comes from the store, not the client.
- **Answer principles** added to SYSTEM: lead with the answer; the shape follows the question; no padding, no narration of steps; SQL only if asked (it is in the tool card); say the as_of / window of the data; if something cannot be known from the views, say so in one line.

## Frontend

- `src/config/apps.ts` — new app `ask` (basename `/ietools/ask`, nav: New chat). Vite proxy `/ietools/ask/api → /api`.
- `src/components/ai-elements/` — installed by `npx ai-elements@latest add conversation message response tool reasoning prompt-input actions loader`.
- `src/components/ask/Chat.tsx` — the conversation: `useChat({ id: threadId, messages: initial, transport })`; user parts in a bubble (`MessageContent` contained variant), assistant parts flat (`Response` = streaming markdown: tables, headers, lists, links, code); `Tool` cards folded by default; `Actions` under each assistant message: thumbs up / down → `POST /feedback`; down opens a small textarea (optional). Empty state: three example questions from the pool.
- `src/pages/ask/AskPage.tsx` — sidebar (threads list, new chat, rename, delete) + `Chat`. Thread id in the URL (`/ask/t/:id`).
- `src/components/ask/AskDrawer.tsx` — the floating bubble (bottom-right) + `Sheet` (70vw) with the same `Chat` inside, latest thread; mounted in the shell for every app that includes `ask`.
- `src/lib/ask/askApi.ts` — threads + feedback calls. The global fetch patch in `useCurrentUser.ts` adds the bearer token to anything under `/api/`, so `useChat`'s fetch is authenticated without extra code.
- Look: the IE-Pulse theme tokens; `@tailwindcss/typography` prose for the answer; no bubbles, no avatars on the assistant side; a thin "thinking / ran SQL" line while a round runs.

## Testing

- Backend (plain-python runner, `tests/test_universe.py`): `stream.sse()` turns a fixed event list into the exact SSE lines (header included); `threads` round-trips a thread and its parts; the access gate 403s a non-pilot NTID; `eval/run.py` still passes the exam harness test (one loop, two consumers).
- Frontend (vitest): `Chat` renders a stored thread (user bubble, flat assistant, tool card) from fixture parts; feedback click posts the vote.
- Manual: dev server + backend, the nine pool questions through the page.

## Build order

1. Backend: loop.py (lift) → stream.py → threads + tables → router + gate → tests.
2. Frontend: AI Elements install → Chat → AskPage → AskDrawer → app registry + proxy → vitest.
3. Run the pool through the page; fix what reads badly; commit.
