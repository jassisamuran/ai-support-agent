import asyncio
import json
from datetime import datetime
from typing import List, Optional
from uuid import UUID

from app.core.agent import enterprise_agent
from app.database import get_db, settings
from app.middleware.auth import get_current_user
from app.middleware.rate_limit import check_rate_limit
from app.middleware.tenant import get_current_org
from app.models.conversation import Conversation, Message, MessageRole
from app.models.organization import Organization
from app.models.user import User
from app.services.billing_service import check_billing_limit
from app.services.llm_service import llm_service
from app.services.webhook_service import fire_event
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, HTTPException, Request
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
    action_type: Optional[str] = "chat"
    target_message_id: Optional[UUID] = None


class ChatResponse(BaseModel):
    conversation_id: str
    message_id: str
    message: str
    tool_calls_made: List[str] = []
    tokens_used: int
    cost_usd: float
    from_cache: bool
    ui_buttons: Optional[dict] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


@router.post("/message", response_model=ChatResponse)
async def send_message(
    request: ChatRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
):
    # await check_rate_limit(str(current_user.id))
    user_token = http_request.headers.get("Authorization")

    # if not await check_billing_limit(org):
    #     raise HTTPException(
    #         status_code=402,
    #         detail="Monthly token limit reached for your free plan",
    #         headers={"X-Error": "Token limit exceeded"},
    #     )
    NAVIGATION_COMMANDS = {"next", "previous", "prev"}

    is_navigation = (
        request.action_type == "navigation"
        or request.message.lower() in NAVIGATION_COMMANDS
    )
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

        pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
        await pool.enqueue_job(
            "task_ingest_document",
            "conversation.started",
            {"conversation_id": str(conversation.id)},
            str(org.id),
        )
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
    result = await enterprise_agent.run(
        user_message=request.message,
        conversation_history=history,
        user_id=str(current_user.id),
        conversation_id=str(conversation.id),
        org=org,
        db=db,
        context={"auth_token": user_token},
    )
    target_message_id = None

    if is_navigation and request.target_message_id:
        target_message_id = str(request.target_message_id)

    db.add(
        Message(
            org_id=org.id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=request.message,
            message_metadata={
                "type": "navigation",
                "button": request.message.lower(),
                "target_message_id": target_message_id,
            }
            if is_navigation
            else None,
        )
    )
    assistant_message = Message(
        org_id=org.id,
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content=result["response"],
        tokens_used=result["tokens_used"],
        tool_calls=result["tool_calls"],
        from_cache=result["from_cache"],
        message_metadata={
            "ui_buttons": {
                "previous": result.get("previous", False),
                "next": result.get("next", False),
            }
        },
    )
    db.add(assistant_message)

    await db.commit()

    return ChatResponse(
        conversation_id=str(conversation.id),
        message_id=str(assistant_message.id),
        message=result["response"],
        tool_calls_made=[tc["name"] for tc in result["tool_calls"]],
        tokens_used=result["tokens_used"],
        ui_buttons={
            "previous": result.get("previous", ""),
            "next": result.get("next", ""),
        },
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


@router.get("/latest-messages")
async def latest_messages(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
):

    result = await db.execute(
        select(Conversation)
        .where(
            Conversation.user_id == current_user.id,
            Conversation.org_id == org.id,
        )
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )

    conversation = result.scalar_one_or_none()

    if not conversation:
        return {"conversation_id": None, "messages": []}

    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at.desc())
        .limit(20)
    )

    messages = list(reversed(result.scalars().all()))

    return {
        "conversation_id": str(conversation.id),
        "messages": [
            {
                "role": msg.role.value,
                "id": str(msg.id),
                "content": msg.content,
                "type": (msg.message_metadata or {}).get("type"),
                "button": (msg.message_metadata or {}).get("button"),
                "ui_buttons": (msg.message_metadata or {}).get("ui_buttons"),
                "target_message_id": (msg.message_metadata or {}).get(
                    "target_message_id"
                ),
            }
            for msg in messages
        ],
    }
