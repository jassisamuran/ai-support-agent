import json
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from app.database import get_db
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import check_rate_limit
from app.middleware.tenant import get_current_org
from app.models.conversation import Conversation, Message, MessageRole
from app.models.organization import Organization
from app.models.user import User
from app.services.llm_service import llm_service
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    conversation_id: Optional[UUID] = None
    channel: Optional[str] = "web"
    stream: Optional[bool] = False


class ChatResponse(BaseModel):
    conversation_id: str
    message: str
    tool_calls_made: List[str] = []
    tokens_used: int
    cost_usd: float
    from_cache: bool
    created_at: datetime = Field(default_factory=datetime.utcnow)


@router.post("/message", response_model=ChatResponse)
async def send_message(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
):
    await check_rate_limit(str(current_user.id))

    if request.conversation_id:
        db_result = await db.execute(
            select(Conversation).where(
                Conversation.id == request.conversation_id,
                Conversation.org_id == org.id,
                Conversation.user_id == current_user.id,
            )
        )

        conversation = db_result.scalar_one_or_none()

        if not conversation:
            raise HTTPException(status_code=404, detail="Conversation not found")

    else:
        conversation = Conversation(
            org_id=org.id,
            user_id=current_user.id,
            channel=request.channel or "web",
        )
        db.add(conversation)
        await db.flush()

    db_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(20)
    )

    past_messages = list(reversed(db_result.scalars().all()))

    history = [
        {"role": msg.role.value, "content": msg.content}
        for msg in past_messages
        if msg.role in (MessageRole.USER, MessageRole.ASSISTANT)
    ]

    if request.stream:
        return await _stream_response(
            request, conversation, history, current_user, org, db
        )

    messages = [
        {
            "role": "system",
            "content": org.system_prompt or "You are helpful customer support agent.",
        },
        *history,
        {"role": "user", "content": request.message},
    ]

    try:
        llm_result = await llm_service.complete(messages=messages)
    except Exception:
        llm_result = {
            "response": "Sorry, AI service is temporarily unavailable.",
            "tokens_used": 0,
            "tool_calls": [],
            "cost_usd": 0.0,
            "from_cache": False,
        }

    response_text = (
        llm_result["message"].content
        if llm_result and llm_result.get("message")
        else "Sorry, I couldn't generate a response."
    )

    tokens_used = llm_result.get("total_tokens", 0)
    tool_calls = llm_result.get("tool_calls", [])
    from_cache = llm_result.get("from_cache", False)
    cost_usd = llm_result.get("cost_usd", 0.0)
    db.add(
        Message(
            org_id=org.id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=request.message,
        )
    )

    db.add(
        Message(
            org_id=org.id,
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=response_text,
            tokens_used=tokens_used,
            tool_calls=tool_calls,
            from_cache=from_cache,
        )
    )

    await db.commit()

    return ChatResponse(
        conversation_id=str(conversation.id),
        message=response_text,
        tool_calls_made=[tc["name"] for tc in tool_calls] if tool_calls else [],
        tokens_used=tokens_used,
        cost_usd=cost_usd,
        from_cache=from_cache,
    )


async def _stream_response(request, conversation, history, current_user, org, db):
    """
    Server-Sent Events streaming.
    Frontend receiving tokens one by one - like chatgpt typing effects
    """
    from app.services.llm_service import llm_service

    system_prompt_result = await db.execute(
        select(Organization).where(Organization.id == org.id)
    )

    org_fresh = system_prompt_result.scalar_one()

    messages = [
        {
            "role": "system",
            "content": org_fresh.system_prompt
            or "You are helpful customer support agent.",
        },
        *history,
        {"role": "user", "content": request.message},
    ]

    async def generate():
        full_response = ""
        try:
            stream = await llm_service._call_openai(messages=messages, stream=True)

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_response += delta.content
                    yield f"data: {json.dumps({'token': delta.content})}\n\n"

            db.add(
                Message(
                    org_id=org.id,
                    conversation_id=conversation.id,
                    role=MessageRole.USER,
                    content=request.message,
                )
            )

            db.add(
                Message(
                    org_id=org.id,
                    conversation_id=conversation.id,
                    role=MessageRole.ASSISTANT,
                    content=full_response,
                )
            )
            await db.commit()

            yield f"data: {json.dumps({'done': True, 'conversation_id': str(conversation.id)})}\n\n"

        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream")
