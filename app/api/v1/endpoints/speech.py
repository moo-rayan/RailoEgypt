import logging

from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, Query
from pydantic import BaseModel

from app.core.security import require_authenticated_user
from app.services.speech_service import transcribe_audio

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/speech", tags=["speech"])

_MAX_FILE_SIZE = 5 * 1024 * 1024  # 5 MB hard limit


class TranscriptionResponse(BaseModel):
    text: str


@router.post("/transcribe", response_model=TranscriptionResponse, dependencies=[Depends(require_authenticated_user)])
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = Query(None, description="ISO language hint, e.g. 'ar' or 'en'"),
):
    """
    Receive a short audio clip and return the Whisper transcription.
    Accepts mp3, wav, m4a, webm — max 5 MB.
    """
    if file.content_type and not file.content_type.startswith(("audio/", "video/")):
        raise HTTPException(400, "Unsupported file type. Send an audio file.")

    audio_bytes = await file.read()
    if len(audio_bytes) > _MAX_FILE_SIZE:
        raise HTTPException(413, "File too large. Max 5 MB.")
    if len(audio_bytes) == 0:
        raise HTTPException(400, "Empty audio file.")

    try:
        text = await transcribe_audio(
            audio_bytes=audio_bytes,
            filename=file.filename or "voice.wav",
            language=language,
        )
        return TranscriptionResponse(text=text)
    except Exception as exc:
        logger.exception("Transcription failed: %s", exc)
        raise HTTPException(500, "Transcription failed. Please try again.")
