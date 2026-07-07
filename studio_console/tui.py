# studio_console/tui.py
"""Generic terminal UI primitives - colors, output helpers, menu widgets, prompts.

This module knows nothing about Studio, components, or .env files.
"""

import sys
from typing import NoReturn

# ---------------------------------------------------------------------------
# TTY detection
# ---------------------------------------------------------------------------

_IS_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()

# ---------------------------------------------------------------------------
# Color helpers
# ---------------------------------------------------------------------------


def _c(code: str, text: str) -> str:
    if not _IS_TTY:
        return text
    return f"\033[{code}m{text}\033[0m"


def _red(t: str) -> str:
    return _c("0;31", t)


def _green(t: str) -> str:
    return _c("0;32", t)


def _yellow(t: str) -> str:
    return _c("1;33", t)


def _cyan(t: str) -> str:
    return _c("0;36", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _dim(t: str) -> str:
    return _c("2", t)


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------


def info(msg: str) -> None:
    print(f"{_cyan('▸')} {msg}")


def ok(msg: str) -> None:
    print(f"{_green('✓')} {msg}")


def warn(msg: str) -> None:
    print(f"{_yellow('!')} {msg}")


def warn_header(msg: str) -> None:
    bar = "━" * 52
    print()
    print(_yellow(_bold(bar)))
    print(_yellow(_bold(f"{msg}")))
    print(_yellow(_bold(bar)))
    print()


def error(msg: str) -> None:
    print(f"{_red('✗')} {msg}", file=sys.stderr)


def fatal(msg: str) -> NoReturn:
    error(msg)
    sys.exit(1)


def heading(msg: str) -> None:
    bar = "━" * 52
    print()
    print(_green(_bold(bar)))
    print(_green(_bold(f"  {msg}")))
    print(_green(_bold(bar)))
    print()


# ---------------------------------------------------------------------------
# Navigation exceptions
# ---------------------------------------------------------------------------


class NavBack(Exception):
    """Raised when user selects Back in a menu."""


class NavExit(Exception):
    """Raised when user selects Exit in a menu."""


# ---------------------------------------------------------------------------
# Key constants
# ---------------------------------------------------------------------------

_ITEM_KEYS = "123456789ABCDEFGHIJKLMNOPQSTUVWYZ"

# ---------------------------------------------------------------------------
# Low-level terminal helpers
# ---------------------------------------------------------------------------


def _read_key() -> str:
    """Read a single keypress. Returns special names for arrow keys."""
    import tty
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            return {"[A": "up", "[B": "down", "[C": "right", "[D": "left"}.get(
                seq, "esc"
            )
        if ch in ("\r", "\n"):
            return "enter"
        if ch == " ":
            return "space"
        if ch == "\x03":  # Ctrl-C
            raise KeyboardInterrupt
        if ch == "\x04":  # Ctrl-D
            raise EOFError
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _clear_lines(n: int) -> None:
    """Move cursor up n lines and clear them."""
    for _ in range(n):
        sys.stdout.write("\033[A\033[2K")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Interactive menus
# ---------------------------------------------------------------------------


def _interactive_multi(
    prompt: str,
    options: list[str],
    selected: set[int] | None = None,
    required: bool = False,
    nav: bool = True,
) -> list[int]:
    """Multi-select with arrow keys and space to toggle.

    Returns list of selected 0-based indices.
    When *nav* is True, Back (9) and Exit (0) are available.
    """
    if not _IS_TTY:
        # Fallback for non-interactive
        return _fallback_multi(prompt, options, selected)

    # Append nav item if enabled
    return_idx = None
    n_options = len(options)
    if nav:
        options = list(options)  # don't mutate caller's list
        return_idx = n_options
        options.append("← Back")

    sel = set(selected) if selected else set()
    cursor = 0
    rendered_lines = 0
    total = len(options)

    while True:
        # Clear previous render
        if rendered_lines:
            _clear_lines(rendered_lines)

        lines: list[str] = []
        lines.append(f"{_cyan('▸')} {prompt}")
        lines.append(f"  {_dim('↑↓=navigate  space=toggle  enter=confirm')}")
        for i, opt in enumerate(options):
            label = _ITEM_KEYS[i] if i < len(_ITEM_KEYS) else " "
            num = _dim(f"{label}.")
            if i == return_idx:
                # Return is not toggleable - show like single-select
                pointer = "→" if i == cursor else " "
                lines.append(f"  {pointer} {num} {_dim('○')} {opt}")
            else:
                check = _green("●") if i in sel else _dim("○")
                pointer = "→" if i == cursor else " "
                lines.append(f"  {pointer} {num} {check} {opt}")

        if required and not sel:
            lines.append(f"  {_dim('(select at least one)')}")

        rendered_lines = len(lines)
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

        try:
            key = _read_key()
        except (KeyboardInterrupt, EOFError):
            print()
            raise KeyboardInterrupt

        if key == "up":
            cursor = (cursor - 1) % total
        elif key == "down":
            cursor = (cursor + 1) % total
        elif key == "space":
            if cursor != return_idx:
                if cursor in sel:
                    sel.discard(cursor)
                else:
                    sel.add(cursor)
        elif key == "enter":
            if cursor == return_idx:
                raise NavBack
            if required and not sel:
                continue
            return sorted(sel)
        elif key.upper() == "N":
            sel.clear()
        elif key.upper() in _ITEM_KEYS:
            idx = _ITEM_KEYS.index(key.upper())
            if idx == return_idx:
                raise NavBack
            elif idx < n_options:
                cursor = idx
                if idx in sel:
                    sel.discard(idx)
                else:
                    sel.add(idx)


def _interactive_single(
    prompt: str,
    options: list[str],
    default: int = 0,
    nav: bool = True,
) -> int:
    """Single-select with arrow keys. Returns 0-based index.

    When *nav* is True (default), Back (9) and Exit (0) are appended.
    Back raises ``NavBack``; Exit raises ``NavExit``.
    Set *nav=False* for menus that manage their own Back/Exit (config menu).
    """
    if not _IS_TTY:
        return _fallback_single(prompt, options, default)

    # Append nav items if enabled
    return_idx = None
    if nav:
        options = list(options)  # don't mutate caller's list
        return_idx = len(options)
        options.append("← Back")

    cursor = default
    rendered_lines = 0
    total = len(options)

    while True:
        if rendered_lines:
            _clear_lines(rendered_lines)

        lines: list[str] = []
        lines.append(f"{_cyan('▸')} {prompt}")
        lines.append(f"  {_dim('↑↓=navigate  enter=select')}")
        for i, opt in enumerate(options):
            label = _ITEM_KEYS[i] if i < len(_ITEM_KEYS) else " "
            num = _dim(f"{label}.")
            if i == cursor:
                lines.append(f"  → {num} {_green('●')} {_bold(opt)}")
            else:
                lines.append(f"    {num} {_dim('○')} {opt}")

        rendered_lines = len(lines)
        sys.stdout.write("\n".join(lines) + "\n")
        sys.stdout.flush()

        try:
            key = _read_key()
        except (KeyboardInterrupt, EOFError):
            print()
            raise KeyboardInterrupt

        if key == "up":
            cursor = (cursor - 1) % total
        elif key == "down":
            cursor = (cursor + 1) % total
        elif key == "enter":
            if cursor == return_idx:
                raise NavBack
            return cursor
        elif key.upper() in _ITEM_KEYS:
            idx = _ITEM_KEYS.index(key.upper())
            if idx < total:
                if idx == return_idx:
                    raise NavBack
                return idx


def _interactive_yn(prompt: str, default: bool = True, nav: bool = True) -> bool:
    """Yes/no with arrow keys."""
    options = ["Yes", "No"]
    result = _interactive_single(prompt, options, default=0 if default else 1, nav=nav)
    return result == 0


# ---------------------------------------------------------------------------
# Fallbacks for non-TTY (pipes, CI)
# ---------------------------------------------------------------------------


def _fallback_multi(
    prompt: str, options: list[str], selected: set[int] | None = None
) -> list[int]:
    print(f"\n{_cyan('▸')} {prompt}")
    for i, opt in enumerate(options, 1):
        marker = " *" if selected and (i - 1) in selected else ""
        print(f"  {_bold(str(i))}. {opt}{marker}")
    while True:
        raw = _prompt("Enter numbers (comma-separated)")
        try:
            picks = [int(x.strip()) for x in raw.split(",") if x.strip()]
            if all(1 <= p <= len(options) for p in picks) and picks:
                return [p - 1 for p in picks]
        except ValueError:
            pass
        print(f"  {_red('✗')} Enter valid numbers between 1 and {len(options)}")


def _fallback_single(prompt: str, options: list[str], default: int = 0) -> int:
    print(f"\n{_cyan('▸')} {prompt}")
    for i, opt in enumerate(options, 1):
        marker = " (default)" if i - 1 == default else ""
        print(f"  {_bold(str(i))}. {opt}{marker}")
    while True:
        raw = _prompt("Enter number", str(default + 1))
        try:
            pick = int(raw.strip())
            if 1 <= pick <= len(options):
                return pick - 1
        except ValueError:
            pass
        print(f"  {_red('✗')} Enter a number between 1 and {len(options)}")


# ---------------------------------------------------------------------------
# Text prompts
# ---------------------------------------------------------------------------


def _prompt(prompt: str, default: str = "") -> str:
    """Text prompt with optional default."""
    suffix = f" [{default}]" if default else ""
    # No color: readline renders raw SGR literally on dumb terminals.
    try:
        answer = input(f"▸ {prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise KeyboardInterrupt
    return answer or default


def _prompt_password(prompt: str) -> str:
    """Prompt for a password with validation and confirmation."""
    import getpass

    print(f"\n{_cyan('▸')} Password requirements:")
    print(f"    • 8 or more characters")
    print(f"    • At least one uppercase letter")
    print(f"    • At least one lowercase letter")
    print(f"    • At least one digit")
    print(f"    • At least one special character (!@#$%^&*...)")
    print()
    while True:
        try:
            pw = getpass.getpass(f"▸ {prompt}: ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise KeyboardInterrupt
        from .env import validate_password

        valid, msg = validate_password(pw)
        if not valid:
            print(f"  {_red('✗')} {msg}")
            continue
        try:
            pw2 = getpass.getpass(f"▸ Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print()
            raise KeyboardInterrupt
        if pw != pw2:
            print(f"  {_red('✗')} Passwords do not match")
            continue
        return pw
