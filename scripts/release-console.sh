#!/usr/bin/env bash
# scripts/release.sh — cut a new GitHub release for studio-console.
#
# Bumps VERSION, commits + pushes, tags, builds the wheel, and creates a
# GitHub release with the wheel attached. Notes are generated from commit
# messages since the previous tag via git-cliff. Override with --message
# or --notes-from.
#
# Usage:
#   scripts/release-console.sh 1.0.1
#   scripts/release-console.sh 1.0.1 --notes-from path/to/notes.md
#   scripts/release-consoe.sh 1.0.1 --message "Quick patch — see commits"
#   scripts/release-console.sh 1.0.1 --dry-run
#
# Requirements:
#   - clean working tree (no uncommitted changes)
#   - on main branch
#   - gh CLI authenticated as a user with push access
#   - python3 -m build available (auto-installed if missing)

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
cd "$REPO_DIR"

# ----- args -----

BUMP_ARG="${1:-}"          # patch | minor | major | X.Y.Z
NOTES_FROM=""
MESSAGE=""
DRY_RUN=0
FORCE=0

shift_args() {
    # Drop $1 (the bump arg, already captured into BUMP_ARG above), then
    # walk the rest. Two-arg flags (--message, --notes-from) shift twice;
    # bare flags shift once. Loop exits when args are exhausted.
    [[ $# -gt 0 ]] && shift
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --notes-from) NOTES_FROM="${2:-}"; shift 2 ;;
            --message)    MESSAGE="${2:-}"; shift 2 ;;
            --dry-run)    DRY_RUN=1; shift ;;
            --force)      FORCE=1; shift ;;
            *)            echo "Unknown arg: $1" >&2; exit 1 ;;
        esac
    done
}
shift_args "$@"

if [[ -z "$BUMP_ARG" ]]; then
    cat >&2 <<EOF
Usage: $0 <bump> [--notes-from path] [--message text] [--dry-run] [--force]

<bump> is one of:
  patch                          1.0.0 → 1.0.1   (bug fixes, no behavior change)
  minor                          1.0.0 → 1.1.0   (new feature, backwards-compatible)
  major                          1.0.0 → 2.0.0   (breaking change)
  X.Y.Z                          explicit version (use sparingly)

Examples:
  $0 patch
  $0 minor --message "Split-hostname support"
  $0 patch --notes-from path/to/notes.md
  $0 patch --dry-run
  $0 1.0.0 --force --message "Re-cut release with corrected wheel"

--force deletes an existing tag + GitHub release before re-creating them.
Use only when you need to replace a release you just published — operators
who already installed the previous wheel will have a stale copy.

Notes (priority order):
  1. --message <text>            inline string
  2. --notes-from <path>          read from file
  3. git-cliff                    auto-generated from commits since last tag
EOF
    exit 1
fi

# Read current version up-front — needed both for explicit-version validation
# and for computing patch/minor/major bumps.
[[ -f VERSION ]] || { echo "✗ VERSION file not found at repo root" >&2; exit 1; }
CURRENT_VERSION="$(cat VERSION | tr -d '[:space:]')"
if ! [[ "$CURRENT_VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "✗ VERSION file has unexpected shape: $CURRENT_VERSION" >&2
    exit 1
fi
IFS=. read -r CUR_MAJ CUR_MIN CUR_PAT <<< "$CURRENT_VERSION"

# Resolve <bump> to a concrete VERSION string.
case "$BUMP_ARG" in
    patch) VERSION="${CUR_MAJ}.${CUR_MIN}.$((CUR_PAT + 1))" ;;
    minor) VERSION="${CUR_MAJ}.$((CUR_MIN + 1)).0" ;;
    major) VERSION="$((CUR_MAJ + 1)).0.0" ;;
    *)
        if [[ "$BUMP_ARG" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
            VERSION="$BUMP_ARG"
        else
            echo "✗ <bump> must be patch, minor, major, or X.Y.Z (got: $BUMP_ARG)" >&2
            exit 1
        fi
        ;;
esac

TAG="v$VERSION"

# ----- helpers -----

red()    { printf '\033[31m%s\033[0m\n' "$*"; }
green()  { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
dim()    { printf '\033[2m%s\033[0m\n' "$*"; }

step()  { printf '\n\033[36m▸\033[0m %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
fatal() { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

run() {
    if [[ "$DRY_RUN" == "1" ]]; then
        dim "    [dry-run] $*"
    else
        "$@"
    fi
}

# ----- preflight -----

step "Preflight"

command -v gh >/dev/null || fatal "gh CLI not installed"
gh auth status >/dev/null 2>&1 || fatal "gh CLI not authenticated — run 'gh auth login'"
ok "gh CLI authed"

ok "current version: $CURRENT_VERSION"
ok "new version:     $VERSION  ($BUMP_ARG)"

if [[ "$CURRENT_VERSION" == "$VERSION" && "$FORCE" == "0" ]]; then
    fatal "VERSION is already $VERSION — nothing to bump (use --force to re-release)"
fi

# Working tree must be clean (so we don't accidentally release with stray edits).
if ! git diff --quiet || ! git diff --cached --quiet; then
    fatal "working tree has uncommitted changes — commit or stash first"
fi
ok "working tree clean"

# Must be on main.
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [[ "$BRANCH" != "main" ]]; then
    fatal "must be on main branch (currently on: $BRANCH)"
fi
ok "on main"

# Tag must not already exist (locally or remotely) unless --force.
TAG_EXISTS_LOCAL=0
TAG_EXISTS_REMOTE=0
RELEASE_EXISTS=0
if git rev-parse "$TAG" >/dev/null 2>&1; then TAG_EXISTS_LOCAL=1; fi
if git ls-remote --tags origin "refs/tags/$TAG" | grep -q "$TAG"; then TAG_EXISTS_REMOTE=1; fi
if gh release view "$TAG" >/dev/null 2>&1; then RELEASE_EXISTS=1; fi

if [[ "$FORCE" == "0" ]]; then
    if [[ "$TAG_EXISTS_LOCAL" == "1" ]]; then
        fatal "tag $TAG already exists locally — re-run with --force to replace"
    fi
    if [[ "$TAG_EXISTS_REMOTE" == "1" ]]; then
        fatal "tag $TAG already exists on origin — re-run with --force to replace"
    fi
    if [[ "$RELEASE_EXISTS" == "1" ]]; then
        fatal "release $TAG already published — re-run with --force to replace"
    fi
    ok "tag $TAG is free"
else
    ok "force-replace mode (existing tag/release will be deleted)"
fi

# Notes resolution.
if [[ -n "$MESSAGE" ]]; then
    NOTES_SOURCE="--message"
    NOTES_BODY="$MESSAGE"
elif [[ -n "$NOTES_FROM" ]]; then
    [[ -f "$NOTES_FROM" ]] || fatal "notes file not found: $NOTES_FROM"
    NOTES_SOURCE="$NOTES_FROM"
    NOTES_BODY="$(cat "$NOTES_FROM")"
else
    command -v git-cliff >/dev/null || fatal "git-cliff not installed — 'brew install git-cliff' (or pass --message)"
    NOTES_SOURCE="git-cliff (commits since previous tag)"
    NOTES_BODY="$(git cliff --tag "$TAG" --unreleased --strip all 2>/dev/null || true)"
    if [[ -z "$(echo "$NOTES_BODY" | tr -d '[:space:]')" ]]; then
        fatal "git-cliff produced empty notes — no commits since last tag, or none matched parsers (use --message to override)"
    fi
fi
ok "notes from: $NOTES_SOURCE"

# Build tool.
if ! python3 -m build --version >/dev/null 2>&1; then
    yellow "  python3 build module missing — installing"
    run python3 -m pip install --quiet --upgrade build
fi
ok "build tool ready"

# ----- nuke existing release/tag if --force -----

if [[ "$FORCE" == "1" ]]; then
    step "Deleting existing $TAG (force mode)"
    if [[ "$RELEASE_EXISTS" == "1" ]]; then
        # gh release delete also drops the tag from the remote when --cleanup-tag is used.
        run gh release delete "$TAG" --yes --cleanup-tag
        ok "deleted GitHub release $TAG"
    elif [[ "$TAG_EXISTS_REMOTE" == "1" ]]; then
        run git push origin ":refs/tags/$TAG"
        ok "deleted remote tag $TAG"
    fi
    # Delete the local tag unconditionally — the TAG_EXISTS_LOCAL snapshot is
    # from preflight and can be stale (e.g. a tag created between runs). `|| true`
    # keeps a missing tag from aborting the whole release under `set -e`.
    if [[ "$DRY_RUN" == "0" ]]; then
        git tag -d "$TAG" 2>/dev/null && ok "deleted local tag $TAG" || true
    fi
    # If VERSION already equals the new version, our bump step would no-op.
    # Reset the file write so the commit step still has something to commit.
    if [[ "$CURRENT_VERSION" == "$VERSION" ]]; then
        ok "VERSION unchanged — skipping bump commit"
        SKIP_VERSION_COMMIT=1
    fi
fi

SKIP_VERSION_COMMIT="${SKIP_VERSION_COMMIT:-0}"

# ----- bump VERSION -----

step "Bumping VERSION → $VERSION"
if [[ "$DRY_RUN" == "1" ]]; then
    dim "    [dry-run] echo $VERSION > VERSION"
else
    echo "$VERSION" > VERSION
fi
ok "VERSION written"

# ----- bump install URLs in README -----
#
# README has one or more lines like:
#   uv tool install [--force] https://github.com/.../releases/download/v1.0.0/studio_console-1.0.0-py3-none-any.whl
# Rewrite the v<X.Y.Z>/studio_console-<X.Y.Z>-py3-none-any.whl portion in
# place. macOS sed needs `sed -i ''`; Linux is `sed -i`. Use a backup
# extension and delete it after to work on both.

step "Updating README install URL"
if [[ "$DRY_RUN" == "1" ]]; then
    dim "    [dry-run] sed-replace v<X.Y.Z>/studio_console-<X.Y.Z> in README.md"
else
    if [[ -f README.md ]]; then
        sed -i.bak -E \
            "s|/releases/download/v[0-9]+\\.[0-9]+\\.[0-9]+/studio_console-[0-9]+\\.[0-9]+\\.[0-9]+-py3-none-any\\.whl|/releases/download/v${VERSION}/studio_console-${VERSION}-py3-none-any.whl|g" \
            README.md
        rm -f README.md.bak
        ok "README.md install URL → v${VERSION}"
    else
        yellow "  README.md not found — skipping URL bump"
    fi
fi

# ----- commit + push -----

if [[ "$SKIP_VERSION_COMMIT" == "1" ]]; then
    step "Skipping commit (VERSION unchanged in --force mode)"
else
    step "Committing + pushing"
    run git add VERSION README.md
    run git commit -m "Release v$VERSION"
    run git push origin main
    ok "committed and pushed"
fi

# ----- tag -----

step "Tagging $TAG"
run git tag -a "$TAG" -m "$TAG"
run git push origin "$TAG"
ok "tagged"

# ----- build wheel -----

step "Building wheel"
run rm -rf dist build studio_console.egg-info
run python3 -m build --wheel

WHEEL=""
if [[ "$DRY_RUN" == "0" ]]; then
    WHEEL="dist/studio_console-${VERSION}-py3-none-any.whl"
    [[ -f "$WHEEL" ]] || fatal "expected wheel not found: $WHEEL"
fi
ok "wheel: ${WHEEL:-(skipped in dry-run)}"

# ----- create release -----

step "Creating GitHub release $TAG"

# Append a standard install snippet so operators always have the right command.
INSTALL_SNIPPET=$(cat <<EOF

## Install

\`\`\`sh
uv tool install --force https://github.com/selfhosthub/studio-console/releases/download/${TAG}/studio_console-${VERSION}-py3-none-any.whl
\`\`\`
EOF
)
FULL_NOTES="${NOTES_BODY}${INSTALL_SNIPPET}"

if [[ "$DRY_RUN" == "1" ]]; then
    dim "    [dry-run] gh release create $TAG <wheel> --title $TAG --notes <body>"
    echo
    yellow "── dry-run notes preview ──"
    echo "$FULL_NOTES"
else
    gh release create "$TAG" "$WHEEL" --title "$TAG" --notes "$FULL_NOTES"
fi
ok "release published"

echo
green "✓ v$VERSION released"
echo
echo "    https://github.com/selfhosthub/studio-console/releases/tag/$TAG"
echo
echo "    To install:"
echo "    uv tool install --force https://github.com/selfhosthub/studio-console/releases/download/$TAG/studio_console-$VERSION-py3-none-any.whl"
