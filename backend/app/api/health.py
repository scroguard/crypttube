from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/health")
async def health(request: Request) -> dict:
    await request.app.state.pool.fetchval("SELECT 1")
    return {"status": "ok"}
