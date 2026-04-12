from fastapi import APIRouter

from app.api.v1.endpoints import account_deletion, admin_audit, admin_auth, admin_chat, admin_live, admin_users, app_config, auth, chat, data_bundle, fares, health, live, notifications, railway, speech, stations, support, train_chat, trains, trips

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(app_config.router)
api_router.include_router(health.router)
api_router.include_router(stations.router)
api_router.include_router(trains.router)
api_router.include_router(trips.router)
api_router.include_router(railway.router)
api_router.include_router(live.router)
api_router.include_router(admin_live.router)
api_router.include_router(admin_chat.router)
api_router.include_router(speech.router)
api_router.include_router(chat.router)
api_router.include_router(train_chat.router)
api_router.include_router(data_bundle.router)
api_router.include_router(notifications.router)
api_router.include_router(support.router)
api_router.include_router(account_deletion.router)
api_router.include_router(auth.router)
api_router.include_router(admin_auth.router)
api_router.include_router(admin_audit.router)
api_router.include_router(admin_users.router)
api_router.include_router(fares.router)
