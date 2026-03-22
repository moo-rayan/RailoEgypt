from fastapi import APIRouter

router = APIRouter()


@router.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok", "service": "TrainLiveEG API"}
