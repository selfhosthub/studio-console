# studio_console/constants.py
"""Static constants - component lists, mappings, display groupings."""

# ---------------------------------------------------------------------------
# Component definitions
# ---------------------------------------------------------------------------

# One row per worker type. Component/profile/image/scale mappings below derive
# from this so a worker is added or renamed in exactly one place.
WORKER_CATALOG: list[dict] = [
    {
        "component": "General worker",
        "profile": "worker-general",
        "image": "studio-worker-general",
        "worker_type": "general",
        "scale_var": "SHS_GENERAL_WORKERS",
        "gpu": None,
    },
    {
        "component": "Transfer worker",
        "profile": "worker-transfer",
        "image": "studio-worker-transfer",
        "worker_type": "transfer",
        "scale_var": "SHS_TRANSFER_WORKERS",
        "gpu": None,
    },
    {
        "component": "Audio worker",
        "profile": "worker-audio",
        "image": "studio-worker-audio",
        "worker_type": "audio",
        "scale_var": "SHS_AUDIO_WORKERS",
        "gpu": "TTS inference. CUDA GPU recommended (8+ GB VRAM); falls back to CPU.",
    },
    {
        "component": "Video worker",
        "profile": "worker-video",
        "image": "studio-worker-video",
        "worker_type": "video",
        "scale_var": "SHS_VIDEO_WORKERS",
        "gpu": None,
    },
    {
        "component": "ComfyUI image worker",
        "profile": "worker-comfyui-image",
        "image": "studio-worker-comfyui",
        "worker_type": "comfyui-image",
        "scale_var": "SHS_COMFYUI_IMAGE_WORKERS",
        "gpu": "Proxy only; the GPU lives in your ComfyUI server (SHS_COMFYUI_URL).",
    },
]

ALL_COMPONENTS = ["PostgreSQL", "API", "UI"] + [w["component"] for w in WORKER_CATALOG]

CORE_DEFAULTS = {"PostgreSQL", "API", "UI"}

# Restricted runtime DB role (RLS cutover). The API's bootstrap provisions it
# from SHS_DATABASE_APP_URL's credentials on every boot — console only names
# it and supplies the URL; it never runs role/grant SQL itself.
APP_DB_ROLE = "shs_app"

# ---------------------------------------------------------------------------
# Component → compose profile mapping
# ---------------------------------------------------------------------------

COMPONENT_TO_PROFILE = {w["component"]: w["profile"] for w in WORKER_CATALOG}

# ---------------------------------------------------------------------------
# Component → Docker image mapping
# ---------------------------------------------------------------------------

COMPONENT_TO_IMAGE = {
    "API": "studio-api",
    "UI": "studio-ui",
    **{w["component"]: w["image"] for w in WORKER_CATALOG},
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
SCALE_VARS: dict[str, str] = {w["component"]: w["scale_var"] for w in WORKER_CATALOG}

# Env var name → compose service name (for --scale flags)
SCALE_PROFILES: dict[str, str] = {w["scale_var"]: w["profile"] for w in WORKER_CATALOG}

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
        "SHS_DATABASE_APP_URL",
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
        "CONSOLE_AUDIO_GPU_DEVICE",
        *[w["scale_var"] for w in WORKER_CATALOG],
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
