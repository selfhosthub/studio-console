# studio_console/constants.py
"""Static constants - component lists, mappings, display groupings."""

# ---------------------------------------------------------------------------
# Component definitions
# ---------------------------------------------------------------------------

ALL_COMPONENTS = [
    "PostgreSQL",
    "API",
    "UI",
    "General worker",
    "Transfer worker",
]

CORE_DEFAULTS = {"PostgreSQL", "API", "UI"}

# ---------------------------------------------------------------------------
# Component → compose profile mapping
# ---------------------------------------------------------------------------

COMPONENT_TO_PROFILE = {
    "General worker": "worker-general",
    "Transfer worker": "worker-transfer",
    "Audio worker": "worker-audio",
    "Video worker": "worker-video",
    "ComfyUI image worker": "worker-comfyui-image",
    "ComfyUI video worker": "worker-comfyui-image",
}

# ---------------------------------------------------------------------------
# Component → Docker image mapping
# ---------------------------------------------------------------------------

COMPONENT_TO_IMAGE = {
    "API": "studio-api",
    "UI": "studio-ui",
    "General worker": "studio-worker-general",
    "Transfer worker": "studio-worker-transfer",
    "Audio worker": "studio-worker-audio",
    "Video worker": "studio-worker-video",
    "ComfyUI image worker": "studio-worker-comfyui",
    "ComfyUI video worker": "studio-worker-comfyui",
}

# Image name → (Dockerfile relative to repo root, build context)
IMAGE_BUILD_CONFIG = {
    "studio-api": ("api/Dockerfile", "api/"),
    "studio-ui": ("ui/Dockerfile", "ui/"),
    "studio-worker-general": ("workers/engines/general/Dockerfile", "workers/"),
    "studio-worker-transfer": ("workers/engines/transfer/Dockerfile", "workers/"),
    "studio-worker-video": ("workers/engines/video/Dockerfile", "workers/"),
    "studio-worker-audio": ("workers/engines/audio/Dockerfile", "workers/"),
    "studio-worker-comfyui": ("workers/engines/comfyui/Dockerfile", "workers/"),
}

THIRD_PARTY_IMAGES = [
    "pgvector/pgvector:0.8.3-pg18-trixie@sha256:6232c5ea178707f278d060b51747c30f310164595674e95d5f9d100f4a48b56c"
]

# ---------------------------------------------------------------------------
# Worker scaling - component name ↔ env var ↔ compose service
# ---------------------------------------------------------------------------

# Component name → env var name
SCALE_VARS: dict[str, str] = {
    "General worker": "SHS_GENERAL_WORKERS",
    "Transfer worker": "SHS_TRANSFER_WORKERS",
    "Audio worker": "SHS_AUDIO_WORKERS",
    "Video worker": "SHS_VIDEO_WORKERS",
    "ComfyUI image worker": "SHS_COMFYUI_IMAGE_WORKERS",
    "ComfyUI video worker": "SHS_COMFYUI_VIDEO_WORKERS",
}

# Env var name → compose service name (for --scale flags)
SCALE_PROFILES: dict[str, str] = {
    "SHS_GENERAL_WORKERS": "worker-general",
    "SHS_TRANSFER_WORKERS": "worker-transfer",
    "SHS_AUDIO_WORKERS": "worker-audio",
    "SHS_VIDEO_WORKERS": "worker-video",
    "SHS_COMFYUI_IMAGE_WORKERS": "worker-comfyui-image",
    "SHS_COMFYUI_VIDEO_WORKERS": "worker-comfyui-image",
}

# Reverse: env var name → component name (for restoring state from .env)
SCALE_VARS_REVERSE: dict[str, str] = {v: k for k, v in SCALE_VARS.items()}

# ---------------------------------------------------------------------------
# .env display grouping (for `show .env` command)
# ---------------------------------------------------------------------------

ENV_SECTIONS = {
    "Required Secrets": [
        "SHS_JWT_SECRET_KEY",
        "SHS_WORKER_SHARED_SECRET",
        "SHS_CREDENTIAL_ENCRYPTION_KEY",
    ],
    "URLs": [
        "SHS_API_BASE_URL",
        "SHS_PUBLIC_API_URL",
        "SHS_PUBLIC_BASE_URL",
        "SHS_FRONTEND_URL",
        "SHS_CORS_ORIGINS",
        "SHS_WS_URL",
    ],
    "Database": [
        "SHS_DATABASE_URL",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "POSTGRES_PORT",
    ],
    "Workspace": [
        "SHS_WORKSPACE_ROOT",
        "SHS_WORKSPACE_DIR",
        "SHS_DB_DATA",
        "SHS_STORAGE_ROOT",
        "SHS_MODELS_ROOT",
        "CONSOLE_BACKUP_ROOT",
    ],
    "Version": ["SHS_STUDIO_VERSION"],
    "API Settings": [
        "SHS_ENV",
        "SHS_DEBUG",
        "PORT",
        "SHS_LOG_LEVEL",
        "SHS_LOG_FORMAT",
        "SHS_ENABLE_ACCESS_LOGS",
        "SHS_SUPPRESS_WORKER_POLLING_LOGS",
        "SHS_STORAGE_BACKEND",
        "SHS_MAINTENANCE_MODE",
        "SHS_COMMUNITY_SOURCE",
        "SHS_PLUS_SOURCE",
        "SHS_CATALOG_CACHE_HOURS",
    ],
    "Worker Settings": [
        "SHS_COMFYUI_URL",
        "CONSOLE_MODELS_PATH",
        "SHS_WHISPER_MODEL",
        "HF_HOME",
        "SHS_GENERAL_WORKERS",
        "SHS_TRANSFER_WORKERS",
    ],
    "Scaling": [
        "CONSOLE_API_REPLICAS",
        "CONSOLE_UI_REPLICAS",
        "SHS_NGINX_PORT",
    ],
    "Runtime": [
        "CONSOLE_COMPONENTS",
        "COMPOSE_PROFILES",
    ],
    "Cloudflare": [
        "CLOUDFLARE_TUNNEL_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "CLOUDFLARE_TUNNEL_ID",
        "CLOUDFLARE_ZONE_ID",
        "CLOUDFLARE_ACCESS_APP_ID",
    ],
}

# ---------------------------------------------------------------------------
# Secret detection
# ---------------------------------------------------------------------------

SECRET_KEYS = {
    "SHS_JWT_SECRET_KEY",
    "SHS_WORKER_SHARED_SECRET",
    "SHS_CREDENTIAL_ENCRYPTION_KEY",
    "POSTGRES_PASSWORD",
    "SHS_ADMIN_PASSWORD",
    "SHS_ENTITLEMENT_TOKEN",
    "CLOUDFLARE_TUNNEL_TOKEN",
    "CLOUDFLARE_API_TOKEN",
}

# Substrings that mark a key as secret
SECRET_PATTERNS = ("SECRET", "PASSWORD", "ENCRYPTION_KEY", "TUNNEL_TOKEN", "API_TOKEN")
