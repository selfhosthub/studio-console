# studio_console/cli.py
"""Argument parser and main entry point."""

import argparse
import os
import sys
from pathlib import Path

from .commands import (
    cmd_backup,
    cmd_build,
    cmd_config_set,
    cmd_config_unset,
    cmd_health,
    cmd_links,
    cmd_logs,
    cmd_reset_password,
    cmd_restart,
    cmd_restore,
    cmd_self_update,
    cmd_show_config,
    cmd_start,
    cmd_stop,
    cmd_upgrade,
    cmd_workers,
    config_menu,
)
from .commands_container import container_menu
from .commands_kit import cmd_worker_kit
from .commands_launch import (
    cmd_launch_core,
    cmd_launch_full,
    cmd_set_core_db_url,
    cmd_shape_lifecycle,
)


def _shape_target(context: str, args) -> bool:
    return context == "host" and getattr(args, "shape", "split") in ("full", "core")
from .env import _workspace_dir, detect_context, env_path
from .resolve import load_profile
from .tui import NavBack, NavExit
from .wizard import wizard, wizard_non_interactive


def _add_config_inputs(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--secrets-profile",
        help="Secrets profile file (KEY=VALUE) resolved into the workspace .env each boot",
    )
    p.add_argument(
        "--inject",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Declared injection: an explicit config input (repeatable)",
    )
    p.add_argument(
        "--inject-file",
        help="Declared injection from a KEY=VALUE file",
    )


def _add_shape(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--shape",
        choices=["split", "full", "core"],
        default="split",
        help="Deployment shape the lifecycle action targets (default: split)",
    )


def _config_inputs(args) -> tuple[Path | None, dict[str, str]]:
    profile = (
        Path(os.path.expanduser(args.secrets_profile))
        if getattr(args, "secrets_profile", None)
        else None
    )
    explicit: dict[str, str] = {}
    inject_file = getattr(args, "inject_file", None)
    if inject_file:
        path = Path(os.path.expanduser(inject_file))
        if not path.exists():
            print(f"error: --inject-file not found: {path}", file=sys.stderr)
            sys.exit(2)
        explicit.update(load_profile(path))
    for item in getattr(args, "inject", []) or []:
        if "=" not in item:
            print(f"error: --inject expects KEY=VALUE, got: {item}", file=sys.stderr)
            sys.exit(2)
        key, value = item.split("=", 1)
        explicit[key] = value
    return profile, explicit


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="studio-console",
        description="Studio Console — operator management CLI",
    )
    sub = parser.add_subparsers(dest="command")

    p_build = sub.add_parser("build", help="Build Docker images from local source")
    p_build.add_argument(
        "images",
        nargs="*",
        default=None,
        help="Images to build (e.g. api ui worker-general). Omit for all.",
    )

    p_start = sub.add_parser("start", help="Start services")
    _add_config_inputs(p_start)
    _add_shape(p_start)
    p_stop = sub.add_parser("stop", help="Stop services")
    _add_shape(p_stop)

    p_restart = sub.add_parser("restart", help="Restart service or all")
    p_restart.add_argument("service", nargs="?", default=None)
    _add_config_inputs(p_restart)
    _add_shape(p_restart)

    p_health = sub.add_parser("health", help="Health check")
    _add_shape(p_health)

    p_config = sub.add_parser("config", help="Show or set config")
    config_sub = p_config.add_subparsers(dest="config_action")
    p_set = config_sub.add_parser("set", help="Set a config value")
    p_set.add_argument("key")
    p_set.add_argument("value")
    p_unset = config_sub.add_parser("unset", help="Remove a config value")
    p_unset.add_argument("key")

    p_logs = sub.add_parser("logs", help="View logs")
    p_logs.add_argument("service", nargs="?", default=None)

    sub.add_parser("workers", help="List/scale workers")
    sub.add_parser("worker-kit", help="Print setup commands for a worker on another machine or GPU host")
    sub.add_parser("reset-password", help="Reset admin password")

    p_backup = sub.add_parser("backup", help="Backup database + files")
    p_backup.add_argument(
        "what",
        nargs="?",
        choices=["all", "db", "orgs"],
        default="all",
        help="What to back up (default: all)",
    )

    p_restore = sub.add_parser("restore", help="Restore from backup")
    p_restore.add_argument("path", nargs="?", default=None)

    sub.add_parser("upgrade", help="Pull latest Studio version + restart")
    sub.add_parser("links", help="Print service URLs")
    p_launch = sub.add_parser("launch-full", help="Launch the self-contained full image and open its console")
    p_launch.add_argument("--tag", help="Image tag to launch (default: latest)")
    p_launch.add_argument("--workspace", help="Host data dir (default: ~/.studio)")
    _add_config_inputs(p_launch)
    p_launch_core = sub.add_parser("launch-core", help="Launch the core image (external Postgres) and open its console")
    p_launch_core.add_argument("--tag", help="Image tag to launch (default: latest)")
    p_launch_core.add_argument("--workspace", help="Host data dir (default: ~/.studio-core)")
    _add_config_inputs(p_launch_core)
    p_core_db = sub.add_parser("core-db-url", help="Set core's external database URL for the next launch-core")
    p_core_db.add_argument("url", nargs="?", default=None, help="postgresql+asyncpg://user:pass@host:5432/db (prompts if omitted)")
    sub.add_parser("wizard", help="Run setup wizard")
    p_init = sub.add_parser("init", help="Non-interactive setup from declared inputs (undocumented)")
    _add_config_inputs(p_init)
    sub.add_parser("self-update", help="Update studio-console to the latest version")
    sub.add_parser("version", help="Print version")


    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    context = detect_context()
    ef = env_path(context)

    if args.command is None:
        try:
            if context in ("container", "runpod"):
                # In-container mode: entrypoint owns provisioning, console is
                # purely an operator tool. No wizard, no compose-shaped commands.
                container_menu(context, ef)
            else:
                if not ef.exists():
                    if not wizard(context, ef):
                        return  # wizard aborted
                # Always show the main menu (wizard saves .env, then we land here)
                config_menu(context, ef)
        except (NavBack, NavExit, KeyboardInterrupt):
            print()
        return

    dispatch = {
        "build": lambda: cmd_build(ef, args.images if args.images else None),
        "start": lambda: sys.exit(
            0 if (
                cmd_shape_lifecycle(args.shape, "start", profile_path=_config_inputs(args)[0], explicit=_config_inputs(args)[1])
                if _shape_target(context, args)
                else cmd_start(context, ef, *_config_inputs(args))
            ) is not False else 1
        ),
        "stop": lambda: (
            cmd_shape_lifecycle(args.shape, "stop")
            if _shape_target(context, args)
            else cmd_stop(context, ef)
        ),
        "restart": lambda: (
            cmd_shape_lifecycle(args.shape, "restart", profile_path=_config_inputs(args)[0], explicit=_config_inputs(args)[1])
            if _shape_target(context, args)
            else cmd_restart(context, ef, args.service)
        ),
        "health": lambda: (
            cmd_shape_lifecycle(args.shape, "health")
            if _shape_target(context, args)
            else cmd_health(context, ef)
        ),
        "config": lambda: (
            cmd_config_set(ef, args.key, args.value)
            if getattr(args, "config_action", None) == "set"
            else cmd_config_unset(ef, args.key)
            if getattr(args, "config_action", None) == "unset"
            else cmd_show_config(context, ef)
        ),
        "logs": lambda: cmd_logs(context, ef, args.service),
        "workers": lambda: cmd_workers(context, ef),
        "worker-kit": lambda: cmd_worker_kit(context, ef),
        "reset-password": lambda: cmd_reset_password(context, ef),
        "backup": lambda: cmd_backup(context, ef, args.what),
        "restore": lambda: cmd_restore(context, ef, args.path),
        "upgrade": lambda: cmd_upgrade(context, ef),
        "links": lambda: cmd_links(context, ef),
        "launch-full": lambda: sys.exit(
            0 if cmd_launch_full(
                context,
                args.tag,
                Path(os.path.expanduser(args.workspace)) if args.workspace else None,
                *_config_inputs(args),
            ) else 1
        ),
        "launch-core": lambda: sys.exit(
            0 if cmd_launch_core(
                context,
                args.tag,
                Path(os.path.expanduser(args.workspace)) if args.workspace else None,
                *_config_inputs(args),
            ) else 1
        ),
        "core-db-url": lambda: sys.exit(0 if cmd_set_core_db_url(context, args.url) else 1),
        "wizard": lambda: wizard(context, ef),
        "init": lambda: sys.exit(0 if wizard_non_interactive(context, ef, *_config_inputs(args)) else 1),
        "self-update": lambda: cmd_self_update(context),
        "version": lambda: cmd_version(),
    }

    handler = dispatch.get(args.command)
    if handler:
        handler()
    else:
        parser.print_help()


def cmd_version() -> None:
    from . import __version__
    print(f"studio-console {__version__}")
