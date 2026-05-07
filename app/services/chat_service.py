"""
AI Chat Service – Multi-provider with automatic fallback.

Provider priority: OpenAI GPT-4o-mini → Gemini 2.5 Flash → Groq
All providers use OpenAI-compatible API format.

All data comes from the Flutter offline bundle (local_results).
No database queries are performed by this service.

Security:
  • System prompt restricts scope to Egyptian railways only.
  • No raw SQL or DB access from this service.

Cost control:
  • OpenAI is used first for the main assistant replies.
  • Gemini and Groq remain as fallback providers.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, asdict
import re
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
    Priority: OpenAI → Gemini → Groq
    """
    providers: list[ProviderConfig] = []

    if settings.openai_api_key:
        providers.append(ProviderConfig(
            name="openai",
            model="gpt-4o-mini",
            api_key=settings.openai_api_key,
            base_url=None,
        ))

    if settings.gemini_api_key:
        providers.append(ProviderConfig(
            name="gemini",
            model="gemini-2.5-flash",
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        ))

    if settings.groq_api_key:
        providers.append(ProviderConfig(
            name="groq",
            model="llama-3.3-70b-versatile",
            api_key=settings.groq_api_key,
            base_url="https://api.groq.com/openai/v1",
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
_SYSTEM_PROMPT_DATA = (
    "أنت المساعد الذكي لسكك حديد مصر. مهمتك مساعدة المسافرين بإجابات دقيقة ومفيدة.\n\n"
    "تعليمات صارمة جداً عند استلام بيانات القطارات:\n"
    "- إذا كان السؤال عن موعد محدد، اذكر رقم القطار والوقت بالضبط من البيانات\n"
    "- boarding_time = وقت القطار في محطة الركوب، alighting_time = وقت الوصول لمحطة النزول\n"
    "- لحساب زمن الرحلة: احسب الفرق بين وقتي المحطتين من قائمة stops\n"
    "- عند السؤال عن 'أسرع رحلة': قارن أزمنة الرحلات المتاحة واختر الأقصر فعلياً\n"
    "  * إذا كان الوقت بالصيغة HH:MM احسب الفرق بالدقائق\n"
    "  * إذا كان الوقت بالصيغة XhYm (مثل 11h30m) حول لدقائق: (11 × 60) + 30 = 690 دقيقة\n"
    "  * اقارن الأزمنة بالدقائق واختر الأقل\n"
    "- عند السؤال عن 'أول قطار' أو 'آخر قطار': رتب حسب الوقت واختر المناسب\n"
    "- لا تخمن ولا تفترض — استخدم الأرقام الفعلية فقط من البيانات\n\n"
    "قواعد الرد:\n"
    "- رد مباشر وواضح يعتمد على البيانات المرفقة\n"
    "- اذكر أرقام القطارات وأوقاتها بشكل محدد\n"
    "- نص عادي فقط بدون أي تنسيق markdown\n"
    "- ارفض بأدب أي سؤال خارج سياق السكة الحديد المصرية\n"
    "- الرد بالعربية دائماً"
)

_SYSTEM_PROMPT_GENERAL = (
    "أنت المساعد الذكي لسكك حديد مصر. مهمتك مساعدة المسافرين بإجابات دقيقة ومفيدة.\n\n"
    "تعليمات:\n"
    "- أجب بناءً على معرفتك العامة عن سكك حديد مصر فقط\n"
    "- إذا لم تكن متأكداً من الإجابة، قل: 'عذراً، لا أملك معلومات كافية عن هذا السؤال'\n"
    "- لا تخترع بيانات عن مواعيد قطارات أو أسعار — اطلب من المستخدم البحث في التطبيق\n\n"
    "أسلوب الرد:\n"
    "- تفاعلي ومفيد\n"
    "- نص عادي فقط بدون أي تنسيق markdown\n"
    "- الرد بالعربية دائماً"
)


def _parse_duration_to_minutes(duration_str: str) -> int:
    """
    Parse Arabic duration format to minutes.
    Examples:
        "11 س و 30 د" -> 690 minutes
        "12 س و 40 د" -> 760 minutes
        "14 س و 25 د" -> 865 minutes
    """
    if not duration_str:
        return float('inf')

    # Match pattern: "X س و Y د" or "X س" or "Y د"
    hours_match = re.search(r'(\d+)\s*س', duration_str)
    minutes_match = re.search(r'(\d+)\s*د', duration_str)

    hours = int(hours_match.group(1)) if hours_match else 0
    minutes = int(minutes_match.group(1)) if minutes_match else 0

    return (hours * 60) + minutes


def _normalize_arabic(text: str) -> str:
    return (
        text.lower()
        .replace("أ", "ا")
        .replace("إ", "ا")
        .replace("آ", "ا")
        .replace("ٱ", "ا")
        .replace("ى", "ي")
        .replace("ة", "ه")
    )


def _calculate_fastest_train(items: list[dict]) -> dict | None:
    """
    Find the fastest train from the list based on duration.
    Returns the fastest train item or None if no valid items.
    """
    if not items:
        return None

    fastest = None
    fastest_minutes = float('inf')

    for item in items:
        if not isinstance(item, dict):
            continue

        duration_str = item.get('segment_duration') or item.get('full_duration', '')
        minutes = _parse_duration_to_minutes(duration_str)

        if minutes < fastest_minutes:
            fastest_minutes = minutes
            fastest = item

    return fastest


def _is_fastest_query(user_message: str) -> bool:
    normalized = _normalize_arabic(user_message)
    return (
        "اسرع" in normalized
        or "الاسرع" in normalized
        or "اقل مدة" in normalized
        or "اقل مده" in normalized
    )


def _is_duration_query(user_message: str) -> bool:
    normalized = _normalize_arabic(user_message)
    return (
        "وقت قد اي" in normalized
        or "وقت قد ايه" in normalized
        or "كم ساعه" in normalized
        or "كام ساعه" in normalized
        or "المده" in normalized
        or "المدة" in user_message
        or "هاخد وقت" in normalized
        or "تستغرق" in normalized
    )


def _sanitize_history_messages(
    conversation_history: list[dict[str, Any]] | None,
) -> list[dict[str, str]]:
    if not conversation_history:
        return []

    sanitized: list[dict[str, str]] = []
    for item in conversation_history[-3:]:
        role = item.get("role")
        content = item.get("content")
        if role in {"assistant", "user", "system"} and isinstance(content, str) and content.strip():
            sanitized.append({"role": role, "content": content})
    return sanitized


def _extract_recent_tool_context(
    conversation_history: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not conversation_history:
        return None

    for item in reversed(conversation_history):
        if item.get("role") != "assistant":
            continue
        tool_data = item.get("tool_data")
        if isinstance(tool_data, dict) and tool_data:
            reused = dict(tool_data)
            if item.get("tool_used") and "tool_used" not in reused:
                reused["tool_used"] = item["tool_used"]
            return reused
    return None


def _build_fastest_reply(fastest_train: dict, local_results: dict) -> str:
    train_num = str(fastest_train.get("train", ""))
    train_type = fastest_train.get("type", "")
    from_station = local_results.get("from_station") or fastest_train.get("from", "")
    to_station = local_results.get("to_station") or fastest_train.get("to", "")
    departure = fastest_train.get("boarding_time") or fastest_train.get("full_departure", "")
    arrival = fastest_train.get("alighting_time") or fastest_train.get("full_arrival", "")
    duration = fastest_train.get("segment_duration") or fastest_train.get("full_duration", "")

    parts = [f"أسرع قطار من {from_station} إلى {to_station} هو رقم {train_num}"]
    if train_type:
        parts[0] += f" {train_type}"
    details: list[str] = []
    if departure:
        details.append(f"يقوم {departure}")
    if arrival:
        details.append(f"ويصل {arrival}")
    if duration:
        details.append(f"ومدة الرحلة {duration}")
    if details:
        parts.append("، " + "، ".join(details) + ".")
    else:
        parts[0] += "."
    return "".join(parts)


def _build_duration_reply(local_results: dict) -> str:
    items = local_results.get("items", [])
    if not isinstance(items, list) or not items:
        return "لا توجد بيانات متاحة عن مدة الرحلة المطلوبة."

    valid_items: list[tuple[int, dict[str, Any]]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        duration_text = item.get("segment_duration") or item.get("full_duration", "")
        duration_minutes = _parse_duration_to_minutes(duration_text)
        if duration_minutes != float("inf"):
            valid_items.append((duration_minutes, item))

    if not valid_items:
        return "لا توجد بيانات متاحة عن مدة الرحلة المطلوبة."

    valid_items.sort(key=lambda x: x[0])
    min_minutes, fastest_item = valid_items[0]
    max_minutes, slowest_item = valid_items[-1]

    from_station = local_results.get("from_station") or fastest_item.get("from", "")
    to_station = local_results.get("to_station") or fastest_item.get("to", "")
    fastest_train = fastest_item.get("train", "")
    fastest_duration = fastest_item.get("segment_duration") or fastest_item.get("full_duration", "")
    fastest_departure = fastest_item.get("boarding_time") or fastest_item.get("full_departure", "")
    fastest_arrival = fastest_item.get("alighting_time") or fastest_item.get("full_arrival", "")

    if len(valid_items) == 1 or min_minutes == max_minutes:
        details = [f"مدة الرحلة من {from_station} إلى {to_station} هي {fastest_duration}"]
        if fastest_train:
            details[0] += f" على القطار رقم {fastest_train}"
        if fastest_departure:
            details.append(f"يقوم {fastest_departure}")
        if fastest_arrival:
            details.append(f"ويصل {fastest_arrival}")
        return "، ".join(details) + "."

    slowest_duration = slowest_item.get("segment_duration") or slowest_item.get("full_duration", "")
    return (
        f"المدة من {from_station} إلى {to_station} تختلف حسب القطار. "
        f"أسرع رحلة تستغرق {fastest_duration} على القطار رقم {fastest_train}"
        f"{f'، يقوم {fastest_departure}' if fastest_departure else ''}"
        f"{f' ويصل {fastest_arrival}' if fastest_arrival else ''}. "
        f"وبشكل عام الرحلات المتاحة تتراوح مدتها بين {fastest_duration} و {slowest_duration}."
    )


# ---------------------------------------------------------------------------
# Chat with local results (offline bundle from Flutter)
# ---------------------------------------------------------------------------
async def _chat_with_local_results(
    user_message: str,
    conversation_history: list[dict[str, Any]] | None,
    local_results: dict,
) -> dict[str, Any]:
    """
    When Flutter sends pre-searched local results, we inject them as
    context and let the AI analyse and respond freely.
    """
    manager = _get_manager()

    # Calculate fastest train for context
    items = local_results.get("items", [])
    fastest_train = _calculate_fastest_train(items)
    if (
        local_results.get("tool_used") == "search_trips"
        and _is_fastest_query(user_message)
        and fastest_train
    ):
        return {
            "reply": _build_fastest_reply(fastest_train, local_results),
            "tool_used": local_results.get("tool_used", "search_trips"),
            "tool_data": local_results,
            "provider": "computed",
            "cached": False,
        }

    if (
        local_results.get("tool_used") == "search_trips"
        and _is_duration_query(user_message)
    ):
        return {
            "reply": _build_duration_reply(local_results),
            "tool_used": local_results.get("tool_used", "search_trips"),
            "tool_data": local_results,
            "provider": "computed",
            "cached": False,
        }

    fastest_info = ""
    if fastest_train:
        train_num = fastest_train.get('train', 'unknown')
        duration = fastest_train.get('segment_duration') or fastest_train.get('full_duration', 'unknown')
        fastest_info = f"\n=== معلومة محسوبة مسبقاً ===\nالقطار الأسرع هو رقم {train_num} (مدة: {duration})\n===\n"

    results_json = json.dumps(local_results, ensure_ascii=False)
    context_note = (
        "=== البيانات المتاحة فقط ===\n"
        f"{results_json}\n"
        "=== نهاية البيانات ===\n"
        f"{fastest_info}\n"
        "تعليمات صارمة:\n"
        "1. يجب أن تقتصر إجابتك 100٪ على البيانات أعلاه فقط\n"
        "2. عند السؤال عن 'أسرع رحلة': اذكر القطار رقم " + (fastest_train.get('train', '[غير متوفر]') if fastest_train else '[غير متوفر]') + " كالأسرع\n"
        "3. عند السؤال عن 'أول قطار' أو 'آخر قطار': رتب حسب الوقت واختر المناسب\n"
        "4. اذكر أرقام القطارات والأوقات بالضبط كما وردت في البيانات\n"
        "5. إذا لم تجد إجابة في البيانات، قل: 'لا توجد بيانات متاحة عن هذا السؤال'\n"
        "6. لا تضف أي معلومات من خارج البيانات المرفقة\n\n"
        "سؤال المستخدم:\n"
        f"{user_message}"
    )

    messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT_DATA}]
    messages.extend(_sanitize_history_messages(conversation_history))
    messages.append({"role": "user", "content": f"{user_message}\n\n{context_note}"})

    try:
        response, provider_name = await manager.chat_completion(messages=messages)
        reply = response.choices[0].message.content or ""

        # Validation: Check if response references actual train numbers from data
        items = local_results.get("items", [])
        if items:
            # Extract train numbers from data
            train_numbers = set()
            for item in items:
                if isinstance(item, dict):
                    # Try different field names for train number
                    for key in ["train_number", "train_id", "train_number", "id"]:
                        if key in item:
                            val = str(item[key])
                            if val:
                                train_numbers.add(val)
                                break

            # Check if any train number from data appears in the reply
            if train_numbers:
                has_data_reference = any(
                    str(tn) in reply for tn in train_numbers
                )
                if not has_data_reference:
                    # AI didn't reference specific trains from data - likely hallucinating
                    logger.warning(
                        "[HALLUCINATION_DETECTED] Provider %s response doesn't reference any train numbers from data. "
                        "Available: %s",
                        provider_name,
                        list(train_numbers)[:5]
                    )
                    # Re-prompt with stronger instructions
                    retry_messages = messages + [
                        {"role": "assistant", "content": reply},
                        {"role": "user", "content": (
                            "أجب مرة أخرى بناءً على البيانات فقط. "
                            f"اذكر أرقام القطارات المتاحة: {', '.join(list(train_numbers)[:3])}. "
                            "لا تضف معلومات من خارج البيانات."
                        )}
                    ]
                    retry_response, retry_provider = await manager.chat_completion(messages=retry_messages)
                    reply = retry_response.choices[0].message.content or reply

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
    conversation_history: list[dict[str, Any]] | None = None,
    local_results: dict | None = None,
) -> dict[str, Any]:
    """
    Process a user message through multi-provider AI.

    All data comes from the Flutter offline bundle (local_results).
    No database queries are performed by this service.
    No caching — every question gets a fresh, dynamic response.

    Provider priority: OpenAI → Gemini → Groq
    Auto-fallback on rate limits.
    """
    # Reuse the last assistant tool context for follow-up questions when the
    # current message did not produce a fresh local search result.
    if local_results is None:
        local_results = _extract_recent_tool_context(conversation_history)

    # If Flutter sent local offline results, use them as context
    if local_results and (local_results.get("items") or local_results.get("train_id")):
        return await _chat_with_local_results(
            user_message, conversation_history, local_results,
        )

    # No local data — AI responds from general knowledge only
    manager = _get_manager()

    messages: list[dict[str, Any]] = [{"role": "system", "content": _SYSTEM_PROMPT_GENERAL}]

    messages.extend(_sanitize_history_messages(conversation_history))

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
