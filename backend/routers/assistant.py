"""Assistant endpoints: a status probe and a streamed chat turn.

The chat turn is Server-Sent Events rather than a single JSON response because
a reply that runs three commands takes several seconds, and watching the answer
arrive — including which commands it reached for — is most of what makes the
thing feel like part of the terminal instead of a box that thinks in silence.

Conversation state lives in the browser: the Messages API is stateless, the
client already holds the transcript, and nothing here is worth a table until
someone asks to keep chats between sessions.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Iterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from ..assistant import availability, stream_reply
from ..auth import get_current_user
from ..database import get_db
from ..models import User
from ..schemas import ChatRequest

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


@router.get("/status")
def status() -> Dict[str, Any]:
    """Whether the assistant is switched on, and if not, what is missing."""
    return availability()


@router.post("/chat")
def chat(
    payload: ChatRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> StreamingResponse:
    history = [m.model_dump() for m in payload.messages]

    def events() -> Iterator[str]:
        for event in stream_reply(history, db, user):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Stop nginx and friends from buffering the stream into one blob.
            "X-Accel-Buffering": "no",
        },
    )
