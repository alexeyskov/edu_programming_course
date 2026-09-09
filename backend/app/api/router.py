from fastapi import APIRouter

from app.api.ai import router as ai_router
from app.api.attempts import router as attempts_router
from app.api.auth import router as auth_router
from app.api.authoring import router as authoring_router
from app.api.courses import router as courses_router
from app.api.evidence import router as evidence_router
from app.api.foundation import router as foundation_router
from app.api.integrity import router as integrity_router
from app.api.reviews import router as reviews_router
from app.api.system import router as system_router

api_router = APIRouter()
api_router.include_router(foundation_router)
api_router.include_router(auth_router)
api_router.include_router(courses_router)
api_router.include_router(authoring_router)
api_router.include_router(attempts_router)
api_router.include_router(evidence_router)
api_router.include_router(reviews_router)
api_router.include_router(integrity_router)
api_router.include_router(ai_router)
api_router.include_router(system_router)
