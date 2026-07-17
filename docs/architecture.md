# studio-console architecture

> **Audience:** Contributors and operators who want to understand what the console is actually doing under the hood. New users should start at the [README](../README.md).

Studio-console is a Python CLI that wraps Docker Compose for Studio operators. This doc covers the internals: file layout, state flow, compose wiring, and the patterns that govern how features get added.

For related deep dives:

- **Environment variables** — every `SHS_*` and supporting var: [env-vars.md](env-vars.md)
- **Public hostname topology** — single vs split hostnames, Cloudflare Access: [topology.md](topology.md)
- **Hybrid VPS + RunPod deployments** — GPU worker pods on RunPod: [vps-runpod.md](vps-runpod.md)

---

## File map

```
studio_console/
  cli.py                  argparse entry, main(), dispatch
  commands.py             host-mode menu logic, cmd_* functions
  commands_container.py   container/runpod menu (in-container ops)
  wizard.py               SetupState, sections, wizard()
  major_version.py        major-boundary detection + gates
  tui.py                  colors, prompts, _interactive_*
  env.py                  read/write .env, compose_cmd, detect_context
  constants.py            ALL_COMPONENTS, COMPONENT_TO_PROFILE, ENV_SECTIONS
  data/                   bundled JSON (known_baselines.json)
  cloudflare/             CF REST API + wizard
```

## State flow

```
studio-console (no args)
       │
       ▼
 detect_context() → host | container | runpod
       │
   host│        else
       │         └────────────┐
       ▼                      ▼
 ~/.studio/.env exists?   container_menu()
   No │  Yes               (entrypoint owns provisioning)
      │   └──────────┐
      ▼              ▼
  wizard()      config_menu()
      │              │
      └──────┬───────┘
             ▼
    write ~/.studio/.env
    copy docker-compose.yml
    generate nginx/studio.conf
```

All `cmd_*` functions read `.env` fresh on every call via `read_env()` — no in-memory state cache. The wizard writes once on save; the menu re-reads on every render.

## Docker Compose wiring (host context only)

```
~/.studio/                    ← workspace root
  .env                        ← all config; read by compose via --env-file
  docker-compose.yml          ← base services (copied from package on first run)
  docker-compose.override.yml ← multi-replica nginx LB (only when replicas > 1)
  nginx/studio.conf           ← generated upstream blocks
  .bootstrapped               ← marker: first-boot admin setup done
  db/                         ← postgres data       (SHS_DB_DATA)
  storage/                    ← orgs, uploads, outputs (SHS_STORAGE_ROOT → /workspace)
  models/                     ← model files          (SHS_MODELS_ROOT)
  backups/                    ← timestamped backup dirs (CONSOLE_BACKUP_ROOT)
```

The four data subdirs are each their own env-var root so a cloud deploy can swap any
of them (CloudSQL for `SHS_DB_DATA`, GCS for `SHS_STORAGE_ROOT`, network volume for
`SHS_MODELS_ROOT`) without touching app code. Containers still see `/workspace` —
compose maps `${SHS_STORAGE_ROOT}:/workspace`.

Compose command always built as:

```
docker compose \
  -f ~/.studio/docker-compose.yml \
  --env-file ~/.studio/.env \
  [-f ~/.studio/docker-compose.override.yml]   ← appended if it exists
```

Worker profiles are activated via `COMPOSE_PROFILES` in `.env` — compose only starts services whose profile matches. `write_env()` always rewrites `COMPOSE_PROFILES` derived from `state.components`.

## Service topology (Split shape)

```
                    ┌──────────┐
         :80        │          │
  ────────────────► │  nginx   │
                    │          │
                    └────┬─────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
         ┌────────┐           ┌─────────┐
         │  api   │           │   ui    │
         │ :8000  │           │  :3000  │
         └────┬───┘           └─────────┘
              │
        ┌─────▼──────┐
        │  postgres  │
        │   :5432    │
        └────────────┘

Workers connect outbound to API via SHS_API_BASE_URL + SHS_WORKER_SHARED_SECRET
```

**Multi-replica nginx load balancing:**

```
              ┌──────────┐
              │  nginx   │
              └────┬─────┘
         ┌─────────┼─────────┐
         ▼         ▼         ▼
      api-1      api-2     api-3    ← numbered services from override
```

When `CONSOLE_API_REPLICAS > 1` or `CONSOLE_UI_REPLICAS > 1`, the console writes `docker-compose.override.yml` with numbered services and parks the bare `api`/`ui` services under a disabled profile. nginx upstream blocks point to the numbered names. The override is auto-generated on every wizard save; setups needing no override delete it if present.

The override also carries the audio worker's GPU grant: when `CONSOLE_AUDIO_GPU_DEVICE` is set (wizard prompt on selecting the Audio worker), a `worker-audio` block adds an nvidia device reservation (`all` or a specific `device_ids`). Empty = no reservation, the worker runs on CPU.

## First-boot sequence

```
studio-console start
       │
       ▼
docker compose up -d
       │
       ▼
poll GET /health every 2s (120s timeout)
       │
   healthy?
    No │ Yes
       │  └──────────────────────────────────────┐
       ▼                                         ▼
  warn + return                       .bootstrapped exists?
                                       No │  Yes
                                          │   └── done
                                          ▼
                                  create super admin
                                    hash pw (bcrypt via API container)
                                    insert user into postgres directly
                                    POST entitlement token secret via API
                                    write .bootstrapped
```

The `.bootstrapped` marker prevents the admin-creation step from running again on subsequent restarts.

## Patterns and gotchas

### Wizard vs `init`

Both call the same `write_env()` with the same `env_data` dict. The only difference is how the dict gets populated: prompts (wizard) vs environment variables (`init`). Identical output.

### Orphan container problem

When a worker is removed from `CONSOLE_COMPONENTS`, `write_env()` updates `.env` (removes the profile) before the restart runs. Compose no longer claims the old container, so `up -d` silently ignores it — the container keeps running.

**Fix:** always stop workers via `docker ps --filter name=studio-worker-` (direct docker, not compose) before `up -d`. Full restarts also pass `--remove-orphans`.

### `write_env()` atomicity

```
1. Read existing file line-by-line
2. For each KEY=VALUE line: if KEY is in new data, replace value in-place
3. Append any new keys not in the original file
4. Write to temp file with 0600 permissions
5. os.replace() → atomic rename (no partial writes visible)
```

Re-running the wizard or `init` will not scramble a hand-edited `.env` — values update in place, comments and blank lines are preserved.

### Backup format

```
$CONSOLE_BACKUP_ROOT/             ← default ~/.studio/backups/
  studio-20260430_120000/
    database.sql      ← pg_dump --inserts of selfhost_studio (with leading header)
    env_backup        ← copy of ~/.studio/.env at backup time
    orgs.tar.gz       ← tar of $SHS_STORAGE_ROOT/orgs/
```

The dump's leading comment block records `studio_image_tag`, `studio_image_digest`, and the alembic revision parseable from the dump's `INSERT INTO alembic_version` line. Both feed `_restore_preflight` for schema-compatibility tiering.

The restore DB picker globs `**/*.sql` recursively so any filename works — including dev dumps like `db-dump-20260430.sql`.

### Major-version boundary detection

Studio's API refuses to start when the database schema is from a different major version than the running image. The bootstrap script (`api/scripts/bootstrap.py:_check_major_version_compatibility`) calls `sys.exit(1)` and prints a `FATAL` message. Without console-side detection, this surfaces as a generic `/health` timeout.

`studio_console/major_version.py` adds preflight detection at four sites:

| Site | Check |
|---|---|
| `cmd_start` | Before `compose up`: DB rev vs. image major in `SHS_STUDIO_VERSION`. |
| `cmd_upgrade` | After version selection, before `.env` mutation: DB rev vs. target major. |
| `_restore_preflight` | Backup rev (parsed from dump) vs. running image major. Hard block, no override. |
| `config_menu` + `cmd_health` | When `/health` is down: scrape API logs for the FATAL signature, surface "blocked" instead of "starting…". |

Detection works in all three shapes (Split, Core, Full). `read_db_revision` and `scrape_guardrail_failure` branch on `context`: host uses `docker compose exec` / `docker compose logs`; container/runpod use raw `psql` / `supervisorctl tail`.

Bundled `data/known_baselines.json` maps `{major: baseline_alembic_revision}`. Stale-by-design: when a new Studio major ships before the next console release, the bundled map doesn't recognize it — the log-scrape fallback catches the condition post-failure instead of pre-blocking. Mid-chain mismatches (e.g., DB at v1.2 / image at v1.1) also fall through to log-scrape; the bundled map only holds major baselines.

Operator remedy when the block fires: set `SHS_STUDIO_VERSION` back to a tag matching the database's major, or restore a backup compatible with the running image. There is no automated migration tool yet.

### Deployment contexts

`detect_context()` determines the environment at startup:

| Context | Detection | `.env` path | Service management |
|---------|-----------|-------------|-------------------|
| `host` | default | `~/.studio/.env` | `docker compose` |
| `container` | `/.dockerenv` exists | `/workspace/.env` | `supervisorctl` + direct psql |
| `runpod` | `RUNPOD_POD_ID` set | `/workspace/.env` | `supervisorctl` + direct psql |

`host` is the Split shape; `container` covers Core and Full; `runpod` is the Full image running on RunPod (worker pods specifically). The `commands.py` host-mode menu and `commands_container.py` container-mode menu diverge at the top of `cli.py:main()`.

Any new feature must handle all three contexts. Mirror the host/else branching in `_read_current_db_revision` and `major_version.read_db_revision`.

### Credential encryption

Provider API keys stored in the database are encrypted with `SHS_CREDENTIAL_ENCRYPTION_KEY`. When restoring a dump from another environment, the key in `.env` must match the key used when the dump was created — otherwise stored credentials are unrecoverable. Pin the key in your secrets file before running `init`.
