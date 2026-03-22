"""
AI Chat Service – Multi-provider with automatic fallback.

Provider priority: Groq → Gemini 2.5 Flash → OpenAI GPT-4o-mini
All providers use OpenAI-compatible API format.

All data comes from the Flutter offline bundle (local_results).
No database queries are performed by this service.

Security:
  • System prompt restricts scope to Egyptian railways only.
  • No raw SQL or DB access from this service.

Cost control:
  • Free providers (Groq, Gemini) used first.
  • OpenAI used only as last resort.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI, APIStatusError, RateLimitError

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-provider configuration
# ---------------------------------------------------------------------------
@dataclass
class ProviderConfig:
    name: str
    model: str
    api_key: str
    base_url: str | None = None
    max_tokens: int = 1000
    temperature: float = 0.6
    supports_tools: bool = True


def _build_providers() -> list[ProviderConfig]:
    """Build provider list from settings. Skip providers with empty keys.
    Priority: Groq → Gemini → OpenAI
    """
    providers: list[ProviderConfig] = []

    if settings.groq_api_key:
        providers.append(ProviderConfig(
            name="groq",
            model="llama-3.3-70b-versatile",
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
        ))

    if settings.gemini_api_key:
        providers.append(ProviderConfig(
            name="gemini",
            model="gemini-2.5-flash",
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        ))

    if settings.openai_api_key:
        providers.append(ProviderConfig(
            name="openai",
            model="gpt-4o-mini",
            api_key=settings.openai_api_key,
            base_url=None,
        ))

    return providers


# ---------------------------------------------------------------------------
# Provider manager with rate-limit tracking and auto-fallback
# ---------------------------------------------------------------------------
class ProviderManager:
    def __init__(self) -> None:
        self._providers = _build_providers()
        self._clients: dict[str, AsyncOpenAI] = {}
        # cooldown_until timestamp per provider
        self._cooldowns: dict[str, float] = {}
        # Cooldown duration per provider (escalates on repeated failures)
        self._cooldown_durations: dict[str, float] = {}
        logger.info(
            "AI providers configured: %s",
            [p.name for p in self._providers],
        )

    def _get_client(self, provider: ProviderConfig) -> AsyncOpenAI:
        if provider.name not in self._clients:
            kwargs: dict[str, Any] = {"api_key": provider.api_key}
            if provider.base_url:
                kwargs["base_url"] = provider.base_url
            self._clients[provider.name] = AsyncOpenAI(**kwargs)
        return self._clients[provider.name]

    def _is_available(self, name: str) -> bool:
        cooldown = self._cooldowns.get(name, 0)
        if time.time() > cooldown:
            return True
        remaining = int(cooldown - time.time())
        logger.debug("Provider %s on cooldown (%ds remaining)", name, remaining)
        return False

    def _mark_rate_limited(self, name: str) -> None:
        # Escalating cooldown: 60s → 120s → 300s → 600s max
        current = self._cooldown_durations.get(name, 30)
        new_duration = min(current * 2, 600)
        self._cooldown_durations[name] = new_duration
        self._cooldowns[name] = time.time() + new_duration
        logger.warning(
            "Provider %s rate-limited → cooldown %ds", name, int(new_duration)
        )

    def _clear_cooldown(self, name: str) -> None:
        """Reset cooldown on successful call."""
        self._cooldowns.pop(name, None)
        self._cooldown_durations.pop(name, None)

    def _is_rate_limit_error(self, error: Exception) -> bool:
        """Check if error is a rate limit / quota exceeded error."""
        if isinstance(error, RateLimitError):
            return True
        if isinstance(error, APIStatusError):
            if error.status_code in (429, 503):
                return True
            body = str(error.body) if error.body else ""
            if any(kw in body.lower() for kw in ("rate_limit", "quota", "resource_exhausted")):
                return True
        return False

    def get_available_providers(self) -> list[ProviderConfig]:
        return [p for p in self._providers if self._is_available(p.name)]

    async def chat_completion(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
    ) -> tuple[Any, str]:
        """
        Try each available provider in order.
        Returns (response, provider_name).
        """
        errors: list[str] = []

        for provider in self._providers:
            if not self._is_available(provider.name):
                continue

            client = self._get_client(provider)
            try:
                kwargs: dict[str, Any] = {
                    "model": provider.model,
                    "messages": messages,
                    "max_tokens": provider.max_tokens,
                    "temperature": provider.temperature,
                }
                if tools and provider.supports_tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = tool_choice

                logger.info("Trying provider: %s (%s)", provider.name, provider.model)
                response = await client.chat.completions.create(**kwargs)
                self._clear_cooldown(provider.name)
                
                # Log finish reason to detect truncation
                finish_reason = response.choices[0].finish_reason if response.choices else "unknown"
                if finish_reason == "length":
                    logger.warning("Provider %s response TRUNCATED (hit token limit)", provider.name)
                else:
                    logger.info("Provider %s responded OK (finish: %s)", provider.name, finish_reason)
                
                return response, provider.name

            except Exception as e:
                if self._is_rate_limit_error(e):
                    self._mark_rate_limited(provider.name)
                    errors.append(f"{provider.name}: rate-limited")
                else:
                    # Non-rate-limit error → short cooldown and try next
                    logger.error("Provider %s error: %s", provider.name, e)
                    self._cooldowns[provider.name] = time.time() + 10
                    errors.append(f"{provider.name}: {type(e).__name__}")
                continue

        # All providers failed
        logger.error("All AI providers failed: %s", errors)
        raise RuntimeError(f"All AI providers unavailable: {'; '.join(errors)}")


# Singleton manager
_manager: ProviderManager | None = None


def _get_manager() -> ProviderManager:
    global _manager
    if _manager is None:
        _manager = ProviderManager()
    return _manager


# ---------------------------------------------------------------------------
# System prompt – kept short to save tokens
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "أنت المساعد الذكي لسكك حديد مصر. مهمتك مساعدة المسافرين بإجابات دقيقة ومفيدة.\n\n"
    "قدراتك عند استلام بيانات رحلات:\n"
    "- حلل البيانات بنفسك واستخرج الإجابة المناسبة للسؤال\n"
    "- boarding_time هو وقت القطار في محطة ركوب المسافر، alighting_time وقت وصوله لمحطة النزول\n"
    "- لحساب زمن الرحلة الفعلي بين محطتين، احسب الفرق بين وقتي المحطتين من البيانات\n"
    "- قائمة stops تحتوي كل محطات التوقف بمواعيدها، استخدمها للحسابات الدقيقة\n"
    "- عند السؤال عن أسرع أو أبطأ أو أنسب قطار، قارن الأزمنة المحسوبة واعرض النتيجة\n"
    "- إذا وجدت بيانات أسعار fares اذكرها عند الحاجة\n\n"
    "أسلوب الرد:\n"
    "- تفاعلي ومتنوع، لا تكرر نفس الصياغة أو القالب في كل مرة\n"
    "- بطول متوسط يناسب السؤال، لا طويل ممل ولا قصير مخل\n"
    "- نص عادي فقط بدون أي تنسيق markdown (لا نجوم * ولا شرطات - ولا عناوين # ولا أقواس)\n"
    "- لا تخترع بيانات غير موجودة أبداً\n"
    "- ارفض بأدب أي سؤال خارج سياق السكة الحديد المصرية\n"
    "- الرد بالعربية دائماً"
)


# ---------------------------------------------------------------------------
# Chat with local results (offline bundle from Flutter)
# ---------------------------------------------------------------------------
async def _chat_with_local_results(
    user_message: str,
    conversation_history: list[dict[str, str]] | None,
    local_results: dict,
) -> dict[str, Any]:
    """
    When Flutter sends pre-searched local results, we inject them as
    context and let the AI analyse and respond freely.
    """
    manager = _get_manager()

    results_json = json.dumps(local_results, ensure_ascii=False)
    context_note = (
        "البيانات المتاحة من قاعدة بيانات القطارات:\n"
        f"{results_json}\n\n"
        "حلل هذه البيانات وأجب على سؤال المستخدم بشكل مباشر ومفيد. "
        "إذا احتجت حساب الزمن بين محطتين، استخدم مواعيد التوقف المتاحة."
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if conversation_history:
        messages.extend(conversation_history[-5:])
    messages.append({"role": "user", "content": f"{user_message}\n\n{context_note}"})

    try:
        response, provider_name = await manager.chat_completion(messages=messages)
        reply = response.choices[0].message.content or ""

        tool_used = local_results.get("tool_used", "search_trips")
        tool_data = local_results

        logger.info("[%s] Chat with local results — %d items", provider_name, len(local_results.get("items", [])))

        return {
            "reply": reply,
            "tool_used": tool_used,
            "tool_data": tool_data,
            "provider": provider_name,
            "cached": False,
        }
    except Exception as e:
        logger.exception("Chat with local results failed: %s", e)
        return {
            "reply": "عذراً، حدث خطأ. حاول مرة أخرى.",
            "tool_used": None,
            "tool_data": None,
            "provider": None,
            "cached": False,
        }


# ---------------------------------------------------------------------------
# Main chat function — fully offline data, no DB queries
# ---------------------------------------------------------------------------
async def chat(
    user_message: str,
    conversation_history: list[dict[str, str]] | None = None,
    local_results: dict | None = None,
) -> dict[str, Any]:
    """
    Process a user message through multi-provider AI.

    All data comes from the Flutter offline bundle (local_results).
    No database queries are performed by this service.
    No caching — every question gets a fresh, dynamic response.

    Provider priority: Groq → Gemini → OpenAI
    Auto-fallback on rate limits.
    """
    # If Flutter sent local offline results, use them as context
    if local_results and (local_results.get("items") or local_results.get("train_id")):
        return await _chat_with_local_results(
            user_message, conversation_history, local_results,
        )

    # No local data — AI responds from general knowledge only
    manager = _get_manager()

    messages: list[dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]

    if conversation_history:
        history_slice = conversation_history[-5:]
        messages.extend(history_slice)

    messages.append({"role": "user", "content": user_message})

    try:
        response, provider_name = await manager.chat_completion(messages=messages)
        reply = response.choices[0].message.content or ""

        return {
            "reply": reply,
            "tool_used": None,
            "tool_data": None,
            "provider": provider_name,
            "cached": False,
        }

    except RuntimeError as e:
        logger.error("All providers failed: %s", e)
        return {
            "reply": "عذراً، جميع خدمات الذكاء الاصطناعي غير متاحة حالياً. حاول بعد دقيقة.",
            "tool_used": None,
            "tool_data": None,
            "provider": None,
            "cached": False,
        }
    except Exception as e:
        logger.exception("Chat service error: %s", e)
        return {
            "reply": "عذراً، حدث خطأ. حاول مرة أخرى.",
            "tool_used": None,
            "tool_data": None,
            "provider": None,
            "cached": False,
        }
