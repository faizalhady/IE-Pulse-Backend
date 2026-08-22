"""
modules/universe/chat/stream.py
───────────────────────────────
Loop events → the AI SDK "UI message stream" v1 (SSE), so `useChat` and AI
Elements render them unmodified. Reference: ai-sdk.dev/docs/ai-sdk-ui/stream-protocol.

    data: {"type":"start","messageId":…}
    data: {"type":"tool-input-available","toolCallId":…,"toolName":…,"input":{…}}
    data: {"type":"tool-output-available","toolCallId":…,"output":{…}}
    data: {"type":"text-start","id":…} · text-delta · text-end
    data: {"type":"error","errorText":…}
    data: {"type":"finish"}
    data: [DONE]

parts() builds the UIMessage parts the client would have assembled from the same
events — what a reopened chat is rendered from.
"""

from __future__ import annotations

import json
import uuid
from typing import Iterable, Iterator

HEADERS = {
    "Content-Type": "text/event-stream",
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "x-vercel-ai-ui-message-stream": "v1",
}


def _line(chunk: dict) -> str:
    return "data: " + json.dumps(chunk, default=str, separators=(",", ":")) + "\n\n"


def _output(payload: dict) -> dict:
    return {"ok": payload.get("ok"), "rows": payload.get("rows"), "text": payload.get("output_text", "")}


def sse(events: Iterable[tuple], message_id: str | None = None) -> Iterator[str]:
    yield _line({"type": "start", "messageId": message_id or uuid.uuid4().hex})
    for kind, payload in events:
        if kind == "tool_call":
            yield _line({"type": "tool-input-available", "toolCallId": payload["id"],
                         "toolName": payload["name"], "input": payload["args"]})
        elif kind == "tool_result":
            yield _line({"type": "tool-output-available", "toolCallId": payload["id"], "output": _output(payload)})
        elif kind == "text":
            tid = uuid.uuid4().hex
            yield _line({"type": "text-start", "id": tid})
            yield _line({"type": "text-delta", "id": tid, "delta": payload})
            yield _line({"type": "text-end", "id": tid})
        elif kind == "error":
            yield _line({"type": "error", "errorText": payload})
    yield _line({"type": "finish"})
    yield "data: [DONE]\n\n"


def parts(events: Iterable[tuple]) -> list[dict]:
    out: list[dict] = []
    by_id: dict[str, dict] = {}
    for kind, payload in events:
        if kind == "tool_call":
            part = {"type": f"tool-{payload['name']}", "toolCallId": payload["id"], "state": "input-available",
                    "input": payload["args"]}
            by_id[payload["id"]] = part
            out.append(part)
        elif kind == "tool_result":
            part = by_id.get(payload["id"])
            if part is not None:
                part["state"] = "output-available"
                part["output"] = _output(payload)
        elif kind == "text":
            out.append({"type": "text", "text": payload})
    return out
