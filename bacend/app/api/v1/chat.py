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
from app.models.conversation import (
    Conversation,
    ConversationStatus,
    Message,
    MessageRole,
)
from app.models.organization import Organization
from app.models.user import User
from app.services.billing_service import check_billing_limit
from app.services.llm_service import llm_service
from app.services.webhook_service import fire_event
from arq import create_pool
from arq.connections import RedisSettings
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter()


class ConversationRequest(BaseModel):
    conversation_id: Optional[str] = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=5000)
    conversation_id: Optional[UUID] = None
    channel: Optional[str] = "web"
    stream: Optional[bool] = False
    action_type: Optional[str] = "chat"
    target_message_id: Optional[UUID] = None


class PaginationInfo(BaseModel):
    page: int
    total_pages: int
    total_items: int
    next: bool
    previous: bool
    showing: Optional[str] = None


class UIBlock(BaseModel):
    """
    Present only when the agent returns a paginated resource (orders / tickets).

    Shape:
    {
        "type": "orders",
        "data": [...],
        "pagination": { "page": 1, "next": true, "previous": false, ... }
    }
    """

    type: str
    data: List[dict]
    pagination: PaginationInfo


class ChatResponse(BaseModel):
    """
    Unified response shape returned by POST /message.

    For plain-text answers:
        { "message": "...", "ui": null, ... }

    For paginated results (orders / tickets):
        {
            "message": "Found 50 orders. Showing page 1.",
            "ui": {
                "type": "orders",
                "data": [...],
                "pagination": { "page": 1, "next": true, "previous": false }
            },
            ...
        }
    """

    conversation_id: str
    message_id: str
    message: str
    ui: Optional[UIBlock] = None  # ← the key addition
    tool_calls_made: List[str] = []
    tokens_used: int
    cost_usd: float
    from_cache: bool
    created_at: datetime = Field(default_factory=datetime.utcnow)


@router.post("/session")
async def create_chat_session(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
):
    conversation = Conversation(
        org_id=org.id,
        user_id=user.id,
        status=ConversationStatus.ACTIVE,
        channel="web",
    )

    db.add(conversation)
    await db.commit()
    await db.refresh(conversation)

    return {
        "conversation_id": str(conversation.id),
        "status": conversation.status,
        "created_at": conversation.created_at,
    }


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

    agent_message: str = result["message"]
    agent_ui: Optional[dict] = result.get("ui")
    tool_calls: list = result.get("tool_calls", [])

    target_message_id = (
        str(request.target_message_id)
        if is_navigation and request.target_message_id
        else None
    )

    db.add(
        Message(
            org_id=org.id,
            conversation_id=conversation.id,
            role=MessageRole.USER,
            content=request.message,
            message_metadata=(
                {
                    "type": "navigation",
                    "button": request.message.lower(),
                    "target_message_id": target_message_id,
                }
                if is_navigation
                else None
            ),
        )
    )

    assistant_message = Message(
        org_id=org.id,
        conversation_id=conversation.id,
        role=MessageRole.ASSISTANT,
        content=agent_message,
        tokens_used=result.get("tokens_used", 0),
        tool_calls=tool_calls,
        from_cache=result.get("from_cache", False),
        message_metadata={"ui": agent_ui} if agent_ui else None,
    )
    db.add(assistant_message)
    await db.commit()

    ui_block: Optional[UIBlock] = None
    if agent_ui:
        ui_block = UIBlock(
            type=agent_ui["type"],
            data=agent_ui.get("data", []),
            pagination=PaginationInfo(**agent_ui["pagination"]),
        )
    return ChatResponse(
        conversation_id=str(conversation.id),
        message_id=str(assistant_message.id),
        message=agent_message,
        ui=ui_block,
        tool_calls_made=[tc["name"] for tc in tool_calls],
        tokens_used=result.get("tokens_used", 0),
        cost_usd=result.get("cost_usd", 0.0),
        from_cache=result.get("from_cache", False),
    )


async def _stream_response(request, conversation, history, current_user, org, db):
    """
    Server-Sent Events streaming.
    Frontend receives tokens one by one — like ChatGPT typing effects.
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
    conversation_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(get_current_org),
):

    if not conversation_id:
        return {"conversation_id": None, "messages": []}

    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )

    result = await db.execute(stmt)
    messages = result.scalars().all()

    return {
        "conversation_id": conversation_id,
        "messages": [
            {
                "id": str(msg.id),
                "role": msg.role.value,
                "content": msg.content,
                "ui": (msg.message_metadata or {}).get("ui"),
                "type": (msg.message_metadata or {}).get("type"),
                "button": (msg.message_metadata or {}).get("button"),
                "target_message_id": (msg.message_metadata or {}).get(
                    "target_message_id"
                ),
            }
            for msg in messages
        ],
    }
