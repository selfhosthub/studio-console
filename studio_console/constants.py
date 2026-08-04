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
        "SHS_API_HOSTNAME",
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
        "SHS_ALLOWED_QUEUES",
        "SHS_WORKER_QUEUES",
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
# Variable lifecycle manifest
# ---------------------------------------------------------------------------

ENV_CLASS_IDENTITY = "identity"
ENV_CLASS_OPERATIONAL = "operational"

# Every env var the console touches, classified by lifecycle.
# identity: persisted on first boot, never clobbered by a later input
#           (wizard-set domains, data-bound secrets/paths, remote resource ids).
# operational: re-resolved on every boot; a secrets profile wins over the
#              persisted .env.
# tests/test_env_manifest.py fails the build when a var used anywhere in this
# repo is missing here.
ENV_CLASSES: dict[str, str] = {
    # Public origin the deployment answers on
    "SHS_PUBLIC_BASE_URL": ENV_CLASS_IDENTITY,
    "SHS_PUBLIC_API_URL": ENV_CLASS_IDENTITY,
    "SHS_WS_URL": ENV_CLASS_IDENTITY,
    "SHS_FRONTEND_URL": ENV_CLASS_IDENTITY,
    "SHS_CORS_ORIGINS": ENV_CLASS_IDENTITY,
    "CONSOLE_PUBLIC_API_BASE_URL": ENV_CLASS_IDENTITY,
    "SHS_NGINX_PORT": ENV_CLASS_IDENTITY,
    # Data-bound: regenerating or repointing orphans existing data
    "SHS_JWT_SECRET_KEY": ENV_CLASS_IDENTITY,
    "SHS_CREDENTIAL_ENCRYPTION_KEY": ENV_CLASS_IDENTITY,
    "SHS_DATABASE_URL": ENV_CLASS_IDENTITY,
    "POSTGRES_PASSWORD": ENV_CLASS_IDENTITY,
    "SHS_WORKSPACE_DIR": ENV_CLASS_IDENTITY,
    "SHS_WORKSPACE_HOST": ENV_CLASS_IDENTITY,
    "SHS_DB_DATA": ENV_CLASS_IDENTITY,
    "SHS_STORAGE_ROOT": ENV_CLASS_IDENTITY,
    "SHS_MODELS_ROOT": ENV_CLASS_IDENTITY,
    # Remote Cloudflare resources: ids and recorded ingress state
    "CLOUDFLARE_TUNNEL_TOKEN": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_TUNNEL_ID": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_TUNNEL_NAME": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_ZONE_ID": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_ACCOUNT_ID": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_ACCESS_APP_ID": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_ACCESS_API_APP_ID": ENV_CLASS_IDENTITY,
    "CLOUDFLARE_INGRESS_ORIGIN": ENV_CLASS_IDENTITY,
    # Operational: profile-resolved every boot
    "SHS_API_HOSTNAME": ENV_CLASS_OPERATIONAL,
    "SHS_API_BASE_URL": ENV_CLASS_OPERATIONAL,
    "SHS_ADMIN_EMAIL": ENV_CLASS_OPERATIONAL,
    "SHS_ADMIN_PASSWORD": ENV_CLASS_OPERATIONAL,
    "SHS_WORKER_SHARED_SECRET": ENV_CLASS_OPERATIONAL,
    "SHS_SUPERVISOR_USER": ENV_CLASS_OPERATIONAL,
    "SHS_SUPERVISOR_PASSWORD": ENV_CLASS_OPERATIONAL,
    "SHS_DATABASE_APP_URL": ENV_CLASS_OPERATIONAL,
    "POSTGRES_USER": ENV_CLASS_OPERATIONAL,
    "POSTGRES_PORT": ENV_CLASS_OPERATIONAL,
    "SHS_STUDIO_VERSION": ENV_CLASS_OPERATIONAL,
    "SHS_ENV": ENV_CLASS_OPERATIONAL,
    "SHS_DEBUG": ENV_CLASS_OPERATIONAL,
    "SHS_LOG_LEVEL": ENV_CLASS_OPERATIONAL,
    "SHS_LOG_FORMAT": ENV_CLASS_OPERATIONAL,
    "SHS_ENABLE_ACCESS_LOGS": ENV_CLASS_OPERATIONAL,
    "SHS_SUPPRESS_WORKER_POLLING_LOGS": ENV_CLASS_OPERATIONAL,
    "SHS_STORAGE_BACKEND": ENV_CLASS_OPERATIONAL,
    "SHS_MAINTENANCE_MODE": ENV_CLASS_OPERATIONAL,
    "SHS_COMMUNITY_SOURCE": ENV_CLASS_OPERATIONAL,
    "SHS_PLUS_SOURCE": ENV_CLASS_OPERATIONAL,
    "SHS_CATALOG_CACHE_HOURS": ENV_CLASS_OPERATIONAL,
    "SHS_ENTITLEMENT_TOKEN": ENV_CLASS_OPERATIONAL,
    "SHS_WHISPER_MODEL": ENV_CLASS_OPERATIONAL,
    "SHS_COMFYUI_URL": ENV_CLASS_OPERATIONAL,
    "SHS_WORKSPACE_ROOT": ENV_CLASS_OPERATIONAL,
    "SHS_DEPLOYMENT_SHAPE": ENV_CLASS_OPERATIONAL,
    "SHS_FORCE_PRODUCTION": ENV_CLASS_OPERATIONAL,
    "SHS_PUBLISH_INTERNAL_BIND": ENV_CLASS_OPERATIONAL,
    "SHS_WORKER_TYPE": ENV_CLASS_OPERATIONAL,
    "SHS_WORKER_QUEUES": ENV_CLASS_OPERATIONAL,
    "SHS_ALLOWED_QUEUES": ENV_CLASS_OPERATIONAL,
    **{w["scale_var"]: ENV_CLASS_OPERATIONAL for w in WORKER_CATALOG},
    "CONSOLE_API_REPLICAS": ENV_CLASS_OPERATIONAL,
    "CONSOLE_UI_REPLICAS": ENV_CLASS_OPERATIONAL,
    "CONSOLE_AUDIO_GPU_DEVICE": ENV_CLASS_OPERATIONAL,
    "CONSOLE_BACKUP_ROOT": ENV_CLASS_OPERATIONAL,
    "CONSOLE_COMPONENTS": ENV_CLASS_OPERATIONAL,
    "CONSOLE_DEFAULT_ADMIN_EMAIL": ENV_CLASS_OPERATIONAL,
    "CONSOLE_DEFAULT_ADMIN_PASSWORD": ENV_CLASS_OPERATIONAL,
    "CONSOLE_IP_ALLOWLIST": ENV_CLASS_OPERATIONAL,
    "CONSOLE_IP_RESTRICT_MODE": ENV_CLASS_OPERATIONAL,
    "CONSOLE_MODELS_PATH": ENV_CLASS_OPERATIONAL,
    "CONSOLE_REPO_ROOT": ENV_CLASS_OPERATIONAL,
    "CONSOLE_SKIP_PRE_RESTORE_SNAPSHOT": ENV_CLASS_OPERATIONAL,
    "CONSOLE_STORAGE_MODE": ENV_CLASS_OPERATIONAL,
    "CLOUDFLARE_API_TOKEN": ENV_CLASS_OPERATIONAL,
    "COMPOSE_PROJECT_NAME": ENV_CLASS_OPERATIONAL,
    "COMPOSE_PROFILES": ENV_CLASS_OPERATIONAL,
    "PORT": ENV_CLASS_OPERATIONAL,
    "HF_HOME": ENV_CLASS_OPERATIONAL,
}

IDENTITY_VARS = frozenset(
    k for k, v in ENV_CLASSES.items() if v == ENV_CLASS_IDENTITY
)

# Bootstrap-only inputs: consumed in-process, never persisted to the workspace
# .env (bootstrap scrubs any hand-written leftovers after use).
TRANSIENT_VARS = frozenset({
    "SHS_ADMIN_EMAIL",
    "SHS_ADMIN_PASSWORD",
    "CONSOLE_DEFAULT_ADMIN_EMAIL",
    "CONSOLE_DEFAULT_ADMIN_PASSWORD",
    "SHS_ENTITLEMENT_TOKEN",
})

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
