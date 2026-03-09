import time
from enum import Enum

import structlog
import tiktoken
from anthropic import AsyncAnthropic
from app.config import settings
from openai import AsyncOpenAI

logger = structlog.get_logger()


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """
    Prevents cascading failures when openai is down.
    After N failures -> open circuit -> all calls goes to claude
    After timeout ->tries OpenAI again (half-open).
    """

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.open_at = None

    def record_success(self):
        self.failure_count = 0
        self.state = CircuitState.CLOSED

    def record_failure(self):
        self.failure_count += 1
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            self.open_at = time.time()
            logger.warning("Circuit breaker OPENED -- switching to Claude fallback")

    def can_attempt(self):
        if self.state == CircuitState.CLOSED:
            return True

        if self.state == CircuitState.OPEN:
            if time.time() - self.open_at >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker HALF-OPEN -- testing OpenAI recovery")
                return True
            return False
        return True


class LLMService:
    def __init__(self):
        self.openai = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        self.anthropic = (
            AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
            if settings.ANTHROPIC_API_KEY
            else None
        )
        self.cb = CircuitBreaker(
            failure_threshold=settings.CB_FAILURE_THRESHOLD,
            recovery_timeout=settings.CB_RECOVERY_TIMEOUT,
        )
        self.encoding = tiktoken.encoding_for_model("gpt-4o-mini")

    def count_tokens(self, messages):
        """Count token before calling api."""
        total = 0
        for msg in messages:
            content = msg.get("content") or ""
            if isinstance(content, str):
                total = len(self.encoding.encode(content))
            total += 4
        return total

    def calculate_cost(
        self, prompt_tokens: int, complete_tokens: int, model: str = "gpt-4o-mini"
    ) -> float:
        if "claude" in model:
            return (prompt_tokens / 1_000_000 * 3.0) + (
                complete_tokens / 1_000_000 * 15.0
            )

        return (prompt_tokens / 1_000_000 * settings.GPT4O_MINI_INPUT_COST_PER_1M) + (
            complete_tokens / 1_000_000 * settings.GPT4O_MINI_OUTPUT_COST_PER_1M
        )

    async def _call_openai(
        self,
        messages: list,
        tools: list = None,
        stream: bool = False,
        temperature: float = 0.1,
    ):
        kwargs = dict(
            model=settings.OPENAI_MODEL,
            messages=messages,
            temperature=temperature,
            max_tokens=1500,
        )

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

        if stream:
            kwargs["stream"] = True
        return await self.openai.chat.completions.create(**kwargs)

    async def _call_claude(
        self, messages: list, system_prompt: str, temperature: float = 0.1
    ):
        claude_message = []
        for msg in messages:
            if msg["role"] in ("user", "assistant"):
                claude_message.append(
                    {"role": msg["role"], "content": msg.get("content") or ""}
                )

        return await self.anthropic.messages.create(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=1500,
            temperature=temperature,
            system=system_prompt,
            messages=claude_message,
        )

    async def complete(
        self,
        messages: list,
        tools: list = None,
        temperature: float = 0.1,
        stream: bool = False,
    ):
        """Main entry point.Automatically handles:
        - Circuit breaker (fallback to claude)
        - Retry on transient failures
        - Tokens counting & cost calculation
        """

        start_time = time.time()
        used_fallback = None

        if self.cb.can_attempt():
            try:
                response = await self._call_openai(messages, tools, stream, temperature)
                self.cb.record_success()

                if stream:
                    return {"stream": response, "model": settings.OPENAI_MODEL}

                prompt_tokens = response.usage.prompt_tokens
                complete_tokens = response.usage.completion_tokens
                cost = self.calculate_cost(prompt_tokens, complete_tokens)

                return {
                    "message": response.choices[0].message,
                    "finish_reason": response.choices[0].finish_reason,
                    "model": settings.OPENAI_MODEL,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": complete_tokens,
                    "cost_usd": cost,
                    "latency_ms": int((time.time() - start_time) * 1000),
                    "used_fallback": None,
                }

            except Exception as e:
                self.cb.record_failure()
                logger.error(
                    "OpenAI call failed", error=str(e), circuit_state=self.cb.state
                )

                if not self.anthropic:
                    raise

        if not self.anthropic:
            raise RuntimeError("OpenAI circuit open and no Claude fallback configured")

        logger.info("Using Claude fallback")
        used_fallback = settings.ANTHROPIC_MODEL

        system_prompt = next(
            (m["content"] for m in messages if m["role"] == "system"),
            "You are a helpful customer support agent.",
        )

        non_system = [m for m in messages if m["role"] != "system"]

        response = await self._call_claude(non_system, system_prompt, temperature)

        prompt_tokens = response.usage.input_tokens
        completion_tokens = response.usage.output_tokens

        class FakeMessage:
            def __init__(self, content):
                self.content = content
                self.tool_calls = None

        return {
            "message": FakeMessage(response.content[0].text),
            "finish_reason": "stop",
            "model": settings.ANTHROPIC_MODEL,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "cost_usd": self.calculate_cost(prompt_tokens, completion_tokens, "claude"),
            "latency_ms": int((time.time() - start_time) * 1000),
            "used_fallback": used_fallback,
        }


llm_service = LLMService()
