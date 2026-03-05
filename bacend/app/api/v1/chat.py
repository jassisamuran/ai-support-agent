import asyncio
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
from app.services.webhook_service import fire_event
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
    tokens_use: int
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
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == request.conversation_id,
                Conversation.org_id == org.id,
                Conversation.user_id == current_user.id,
            )
        )

        conversation = result.scalar_one_or_none()

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

        # asyncio.create_task(
        #     fire_event(
        #         "conversation.started",
        #         {"conversation_id": str(conversation.id), "channel": request.channel},
        #         str(org.id),
        #     )
        # )

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(20)
    )

    past_messages = list(reversed(result.scalars().all()))

    history = [
        {"role": msg.role.value, "content": msg.content}
        for msg in past_messages
        if msg.role in (MessageRole.USER, MessageRole.ASSISTANT)
    ]

    if request.stream:
        return await _stream_response(request, history, current_user, org, db)

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
            content=result["response"],
            tokens_used=result["tokens_used"],
            tool_calls=result["tool_calls"],
            from_cache=result["from_cache"],
        )
    )

    await db.commit()

    return ChatResponse(
        conversation_id=str(conversation.id),
        message=result["response"],
        tool_calls_made=[tc["name"] for tc in result["tool_calls"]],
        tokens_used=result["tokens_used"],
        cost_usd=result["cost_usd"],
        from_cache=result["from_cache"],
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
        }
        * history,
        {"role": "user", "content": request.message},
    ]

    async def generate():
        full_response = ""
        try:
            result = await llm_service._call_openai(messages=messages, stream=True)
            stream = result["stream"]

            async for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    full_response += delta.content
                    yield f"data: {json.dumps({'tokens': delta.content})}\n\n"

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
