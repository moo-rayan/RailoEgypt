"""
AI Chat endpoint – secured, rate-limited, read-only.
"""

from __future__ import annotations

from pydantic import BaseModel, Field
from fastapi import APIRouter, Depends

from app.core.security import require_authenticated_user
from app.services.chat_service import chat

router = APIRouter(prefix="/chat", tags=["chat"])


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=500)
    history: list[dict[str, str]] = Field(
        default=[],
        max_items=5,
        description="آخر 5 رسائل من المحادثة [{role, content}]",
    )
    local_results: dict | None = Field(
        default=None,
        description="نتائج بحث محلية من الـ offline bundle",
    )


class ChatResponse(BaseModel):
    reply: str
    tool_used: str | None = None
    tool_data: dict | None = None
    provider: str | None = None
    cached: bool = False


@router.post("", response_model=ChatResponse, dependencies=[Depends(require_authenticated_user)])
async def chat_message(body: ChatRequest):
    result = await chat(
        user_message=body.message,
        conversation_history=body.history or None,
        local_results=body.local_results,
    )
    return ChatResponse(**result)
