from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # App
    app_env: str = "development"
    app_secret_key: str = "change-me"
    app_allowed_origins: str = "http://localhost,http://localhost:3000,http://localhost:8000"

    # Supabase
    supabase_url: str
    supabase_anon_key: str
    supabase_service_role_key: str = ""
    supabase_jwt_secret: str = ""

    # Custom app JWT (issued after Supabase auth, long-lived)
    # REQUIRED in production — must be a strong random secret (≥48 chars)
    app_jwt_secret: str = ""
    # Token lifetime in hours (e.g. 720=30d, 168=7d, 24=1d)
    app_token_expiry_hours: int = 720

    # Google Maps
    google_maps_api_key: str = ""

    # AI Providers (fallback order: Groq → Gemini → OpenAI)
    groq_api_key: str = ""
    gemini_api_key: str = ""
    openai_api_key: str = ""

    # Database (direct asyncpg)
    database_url: str

    # WebSocket security (REQUIRED - must be set in .env with strong random value)
    ws_secret_key: str

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    cache_ttl_seconds: int = 300

    # Cloudflare R2 (S3-compatible object storage for bundle)
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_endpoint: str = ""
    r2_bucket: str = "bundle"

    # Data bundle encryption (AES-256 key, base64-encoded 32 bytes)
    bundle_encryption_key: str = ""

    # Firebase Cloud Messaging (base64-encoded service account JSON)
    firebase_credentials_base64: str = ""

    # Admin dashboard
    admin_api_key: str = "change-me-admin-key"

    # Live tracking
    max_active_contributors: int = 5  # max active contributors per train room

    # Pagination
    default_page_size: int = 20
    max_page_size: int = 2000

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.app_allowed_origins.split(",")]

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
