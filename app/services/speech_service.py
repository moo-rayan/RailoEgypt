import io
import logging
from pydub import AudioSegment
from openai import AsyncOpenAI
from app.core.config import settings

logger = logging.getLogger(__name__)

_client: AsyncOpenAI | None = None

# ─── Silence Detection ────────────────────────────────────────────────────────
_MIN_RMS_THRESHOLD = 50  # Minimum RMS energy to consider audio as non-silent
_MIN_AUDIO_SIZE = 1024   # Minimum file size in bytes (< 1KB is likely empty)

# ─── Whisper Hallucination Filter ─────────────────────────────────────────────
_HALLUCINATION_PATTERNS = [
    "توقيت وترجمة",
    "ترجمة نانسي",
    "نانسي قنقر",
    "شكرا على المشاهدة",
    "شكراً على المشاهدة",
    "اشتركوا في القناة",
    "لا تنسوا الاشتراك",
    "السلام عليكم",
    "مشاهدة ممتعة",
    "أرجو أن",
    "ترجمة حفصة",
    "ترجمة سلمى",
    "ترجمه",
    "subtitles by",
    "thank you for watching",
    "subscribe",
    "please subscribe",
    "thanks for watching",
    "amara.org",
    "www.",
    "http",
    "♪",
    "♫",
    "...",
]


def _is_audio_silent(audio_bytes: bytes, filename: str) -> bool:
    """Check if audio is silent by analyzing RMS energy using pydub."""
    try:
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "wav"
        buffer = io.BytesIO(audio_bytes)
        audio = AudioSegment.from_file(buffer, format=ext)
        rms = audio.rms
        logger.info("Audio RMS energy: %d (threshold: %d)", rms, _MIN_RMS_THRESHOLD)
        return rms < _MIN_RMS_THRESHOLD
    except Exception as exc:
        logger.warning("Failed to analyze audio RMS: %s — skipping silence check", exc)
        return False


def _is_hallucination(text: str) -> bool:
    """Check if transcribed text matches known Whisper hallucination patterns."""
    normalized = text.strip().lower()
    if len(normalized) < 3:
        return True
    for pattern in _HALLUCINATION_PATTERNS:
        if pattern.lower() in normalized:
            logger.info("Hallucination detected: '%s' matches '%s'", text, pattern)
            return True
    return False


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


async def transcribe_audio(
    audio_bytes: bytes,
    filename: str = "voice.wav",
    language: str | None = None,
) -> str:
    """
    Send audio bytes to OpenAI Whisper and return the transcribed text.
    Uses whisper-1 for cost efficiency ($0.006/min).
    Prompt hints improve Arabic station name accuracy.

    Includes pre-checks:
    1. File size check — reject tiny files (likely empty/silent)
    2. RMS energy check — reject audio below noise floor
    3. Post-transcription hallucination filter
    """
    # ── Pre-check 1: File size ────────────────────────────────────────────────
    if len(audio_bytes) < _MIN_AUDIO_SIZE:
        logger.info("Audio too small (%d bytes), skipping Whisper call", len(audio_bytes))
        return ""

    # ── Pre-check 2: RMS silence detection ────────────────────────────────────
    if _is_audio_silent(audio_bytes, filename):
        logger.info("Audio is silent (below RMS threshold), skipping Whisper call")
        return ""

    # ── Whisper transcription ─────────────────────────────────────────────────
    client = _get_client()

    prompt_hint = (
        "محطات القطارات المصرية: القاهرة، الإسكندرية، الجيزة، طنطا، "
        "المنصورة، أسيوط، الأقصر، أسوان، بورسعيد، السويس، دمنهور، "
        "بنها، الزقازيق، المنيا، سوهاج، قنا، الفيوم، بني سويف، "
        "شبين الكوم، كفر الشيخ، مرسى مطروح، "
        "Egyptian train stations: Cairo, Alexandria, Giza, Tanta, "
        "Mansoura, Assiut, Luxor, Aswan, Port Said, Suez"
    )

    buffer = io.BytesIO(audio_bytes)
    buffer.name = filename

    transcription = await client.audio.transcriptions.create(
        model="whisper-1",
        file=buffer,
        language=language,
        prompt=prompt_hint,
        response_format="text",
    )

    text = transcription.strip() if isinstance(transcription, str) else transcription.text.strip()
    logger.info("Whisper transcription: %s", text)

    # ── Post-check: Hallucination filter ──────────────────────────────────────
    if _is_hallucination(text):
        logger.info("Filtered hallucinated Whisper output: '%s'", text)
        return ""

    return text
