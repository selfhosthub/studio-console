# studio-console

Operator CLI for managing a self-hosted [Studio](https://github.com/selfhosthub/studio) instance. Wraps Docker Compose to handle setup, configuration, and day-to-day operations.

## Deployment shapes

Studio ships three images. Most operators want **Split**.

- **Split**: multi-container compose stack. Console runs on the host and manages `docker compose`. **This README documents Split.**
- **Core** (`studio-core`): single container bundling API + UI; you bring an external Postgres. Console runs inside the container.
- **Full** (`studio-full`): single container with bundled Postgres.

In Core and Full the container entrypoint provisions Studio on first boot; the console is a diagnostic + ops tool.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Run the Full image](#run-the-full-image)
- [Run the Core image](#run-the-core-image)
- [Daily Operations](#daily-operations)
- [Upgrading](#upgrading)
- [CLI Reference](#cli-reference)
- [Further reading](#further-reading)

---

## Quick Start

[Docker](https://docs.docker.com/get-docker/) + Compose v2 required.

### Interactive

```sh
# 1. Install uv (one-time)
curl -LsSf https://astral.sh/uv/install.sh | sh
source ~/.bashrc           # macOS default shell is zsh, use ~/.zshrc instead

# 2. Install studio-console (isolated CLI, recommended)
uv tool install studio-console

# 3. Run
studio-console
```

No uv and don't want it? `pip install studio-console` works anywhere Python 3.8+ is available. It installs into the active environment instead of an isolated one, so a virtualenv is a good idea.

On first run with no `~/.studio/.env`, the wizard launches. Walk through the sections, save, then go to **Services → Start all**.

The wizard creates `~/.studio/` containing `.env` (0600), `docker-compose.yml`, `nginx/studio.conf`, and four data subdirs:

```
~/.studio/
├── .env, .bootstrapped, docker-compose.yml, nginx/
├── db/         postgres data         (SHS_DB_DATA)
├── storage/    orgs, uploads, outputs (SHS_STORAGE_ROOT, mounted at /workspace in containers)
├── models/     model files            (SHS_MODELS_ROOT)
└── backups/    local DB dumps         (SHS_BACKUP_ROOT)
```

Each data subdir is its own env var, so a cloud deploy can repoint individual roots at CloudSQL, GCS, or a network volume without changing code. **Protect this directory and never delete `.env`.** Two of its values cannot be regenerated:

- `SHS_CREDENTIAL_ENCRYPTION_KEY`: losing it makes all stored provider API keys unrecoverable.
- `POSTGRES_PASSWORD`: set on first DB init; the live database keeps using this value, so a regenerated one will lock the API out.

Back both up separately (password manager, secrets vault) so you can recover if the host or `.env` is ever lost.

### Non-interactive (scripted / CI)

```bash
SHS_CREDENTIAL_ENCRYPTION_KEY=<32+ char key> \
SHS_ADMIN_EMAIL=admin@example.com \
SHS_ADMIN_PASSWORD=<password> \
studio-console init && studio-console start
```

Full list of supported environment variables in [docs/env-vars.md](docs/env-vars.md).

---

## Run the Full image

The **Full** image (`studio-full`) is a single self-contained container with bundled Postgres, API, UI, and workers under supervisord. No Compose stack, no external database. `launch-full` runs it on your machine and drops you into its in-container console.

```sh
# Launch (defaults to the latest tag; workspace defaults to ~/.studio, shared with Split)
studio-console launch-full

# Pin a tag and/or use a separate workspace to run Full alongside a Split install
studio-console launch-full --tag 1.2.4 --workspace ~/.studio-full
```

The workspace (mounted at `/workspace`) holds the generated `.env`, the Postgres data dir, and org files; it persists across restarts. You're prompted once for a supervisor username/password; the console remembers them and re-injects on every launch.

> **Do not point Full at an existing Split `~/.studio` workspace** unless you mean to share it. Split and Full share the same `.env`/encryption key but **must never run concurrently** against the same data. Use `--workspace` to keep them separate.

### Re-entering the console after you exit

Exiting the console **does not stop the container**; it keeps running. `launch-full` won't re-attach to an already-running container; instead, exec back in from the host:

```sh
docker exec -it studio-full studio-console        # re-open the in-container menu

# Or run one-off commands from the host without entering the menu:
docker exec studio-full studio-console health
docker exec studio-full studio-console restart api
docker exec studio-full studio-console logs api
```

Inside the container the console manages services via `supervisorctl` (health, restart, logs, per-service control), plus config, backup, password reset, and Cloudflare setup.

---

## Run the Core image

The **Core** image (`studio-core`) is the same single-container API + UI as Full, but without a bundled database. You bring your own Postgres (a managed cloud instance, a RunPod sidecar, any reachable server). `launch-core` runs it on your machine and drops you into its in-container console.

Point Core at your database first, then launch:

```sh
# Set the external database URL (prompts if you omit it)
studio-console core-db-url postgresql+asyncpg://user:pass@host:5432/studio

# Launch (defaults to the latest tag; workspace defaults to ~/.studio-core)
studio-console launch-core

# Pin a tag and/or use a separate workspace
studio-console launch-core --tag 1.2.4 --workspace ~/.studio-core
```

Precedence for the database URL: a saved value from `core-db-url`, then the `SHS_DATABASE_URL` environment variable, then an interactive prompt. Export the env var to boot Core unattended against a remote Postgres with no prompt:

```sh
SHS_DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/studio studio-console launch-core
```

Re-enter and run one-off commands the same way as Full, swapping the container name:

```sh
docker exec -it studio-core studio-console        # re-open the in-container menu
docker exec studio-core studio-console health
```

---

## Daily Operations

```
Services → Start all      bring everything up
Services → Stop all       bring everything down
Services → Health         check API + worker status
Services → View logs      recent logs (all or per-service)
Services → Stream logs    follow live (Ctrl-C to stop)
Services → Links          open UI and API docs in browser

Backup → Backup all       database + .env + org files
Backup → Restore DB       pick a .sql file from disk
```

Backups land in `~/.studio/backups/studio-YYYYMMDD_HHMMSS/`.

---

## Upgrading

**Studio (the application):**

```
Images → Upgrade
```

Pulls the latest tag from GHCR, updates `SHS_STUDIO_VERSION` in `.env`, and restarts all services. If the new version's major doesn't match your database's major, the upgrade is blocked with a clear message. See [docs/architecture.md](docs/architecture.md#major-version-boundary-detection).

**studio-console itself:**

```bash
studio-console self-update
```

`self-update` detects how Console was installed (uv, pip, or Homebrew) and upgrades in place. Inside a Studio container it upgrades the bundled console the same way; recreating the container reverts to the version baked into the image.

---

## CLI Reference

```bash
studio-console                          # wizard on first run, menu otherwise

studio-console start                    # start all services
studio-console stop                     # stop all services
studio-console restart [service]        # restart all or one service
studio-console health                   # API + worker health check
studio-console logs [service]           # view recent logs
studio-console build [image ...]        # build images from source
studio-console upgrade                  # pull latest version + restart
studio-console backup                   # backup database + files
studio-console restore [path]           # restore from backup directory
studio-console links                    # print service URLs
studio-console config                   # show current .env values
studio-console config set KEY VALUE     # set a single .env value
studio-console config unset KEY         # remove a single .env value
studio-console workers                  # list/scale workers
studio-console reset-password           # reset super admin password
studio-console wizard                   # re-run setup wizard
studio-console init                     # non-interactive setup from env vars
studio-console self-update              # upgrade studio-console itself
studio-console version                  # print version

studio-console launch-full [--tag T] [--workspace DIR]   # run the Full single-container image on the host
studio-console launch-core [--tag T] [--workspace DIR]   # run the Core single-container image (external Postgres)
studio-console core-db-url [URL]        # set Core's external database URL for the next launch-core
```

For the Full or Core image, run operational subcommands through `docker exec studio-full studio-console <cmd>` (or `studio-core`). See [Run the Full image](#run-the-full-image) and [Run the Core image](#run-the-core-image).

---

## Further reading

- **[Architecture](docs/architecture.md)**: file layout, state flow, Compose wiring, first-boot, internals (wizard vs init, orphan workers, backup format, major-version detection, deployment contexts).
- **[Environment variables](docs/env-vars.md)**: every `SHS_*` and supporting var.
- **[Public hostname topology](docs/topology.md)**: single vs split hostnames, Cloudflare tunnel + Access, IP restrictions.
- **[VPS + RunPod deployments](docs/vps-runpod.md)**: hybrid setups with GPU worker pods on RunPod.

---

## License

Studio Console is source-available under the **Studio Console Use License**. It is not open source.

You may install, run, and modify Console to manage your own Studio deployments, and to install and manage deployments for individual clients as service work. You may not redistribute, fork, sublicense, or resell it, and you may not use it as the engine of a service that provisions or vends Studio instances to third parties.

Console is licensed separately from the Studio platform, which is governed by its own [Studio Use License](https://github.com/selfhosthub/studio/blob/main/LICENSE). Installing Console grants no rights in Studio or in marketplace content.

- [LICENSE](LICENSE): the full terms
- [LEGAL.md](LEGAL.md): operator obligations and third-party responsibilities
