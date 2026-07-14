#!/usr/bin/env bash
# install.sh: Studio Console installer
#
# Preferred install method:
#   uv tool install <wheel-url-from-the-latest-release>
#
# This script is the fallback for environments without uv:
#   curl -fsSL https://raw.githubusercontent.com/selfhosthub/studio-console/main/install.sh | bash

set -euo pipefail

REPO="selfhosthub/studio-console"
INSTALL_DIR="${HOME}/.studio-console"
BIN_NAME="studio-console"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

info()  { echo -e "${CYAN}▸${NC} $1"; }
ok()    { echo -e "${GREEN}✓${NC} $1"; }
warn()  { echo -e "${YELLOW}!${NC} $1"; }
fatal() { echo -e "${RED}✗${NC} $1" >&2; exit 1; }

# --- License notice ---
echo ""
echo -e "  Studio Console is source-available under the Studio Console Use License."
echo -e "  It is not open source. Installing it accepts the terms:"
echo -e "  ${CYAN}https://github.com/selfhosthub/studio-console/blob/main/LICENSE${NC}"
echo ""

# --- Python check ---
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null; then
        version=$("$candidate" -c 'import sys; print(sys.version_info[:2] >= (3, 8))' 2>/dev/null || echo "False")
        if [ "$version" = "True" ]; then
            PYTHON="$candidate"
            break
        fi
    fi
done
[ -n "$PYTHON" ] || fatal "Python 3.8+ is required. Install it from https://python.org"
ok "Python: $($PYTHON --version)"

# --- Fetch latest release tag ---
info "Checking latest release..."
API_URL="https://api.github.com/repos/${REPO}/releases/latest"
if command -v curl &>/dev/null; then
    LATEST=$(curl -fsSL "$API_URL" | grep '"tag_name"' | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')
elif command -v wget &>/dev/null; then
    LATEST=$(wget -qO- "$API_URL" | grep '"tag_name"' | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')
else
    fatal "curl or wget is required"
fi
[ -n "$LATEST" ] || fatal "Could not fetch latest release from GitHub"
ok "Latest: ${LATEST}"

# --- Check if already installed and up to date ---
CURRENT_VERSION=""
if [ -f "${INSTALL_DIR}/VERSION" ]; then
    CURRENT_VERSION=$(cat "${INSTALL_DIR}/VERSION")
fi
LATEST_VERSION="${LATEST#v}"

if [ "${CURRENT_VERSION}" = "${LATEST_VERSION}" ]; then
    ok "Already up to date (${CURRENT_VERSION})"
    exit 0
fi

# --- Download ---
TARBALL_URL="https://github.com/${REPO}/archive/refs/tags/${LATEST}.tar.gz"
TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

info "Downloading ${LATEST}..."
if command -v curl &>/dev/null; then
    curl -fsSL "$TARBALL_URL" -o "${TMP_DIR}/release.tar.gz"
else
    wget -qO "${TMP_DIR}/release.tar.gz" "$TARBALL_URL"
fi

# --- Extract ---
mkdir -p "${TMP_DIR}/extracted"
tar -xzf "${TMP_DIR}/release.tar.gz" -C "${TMP_DIR}/extracted" --strip-components=1

# --- Install ---
rm -rf "${INSTALL_DIR}"
cp -r "${TMP_DIR}/extracted" "${INSTALL_DIR}"

# Write install method marker so self-update knows how it was installed
echo "curl" > "${INSTALL_DIR}/.install-method"

ok "Installed to ${INSTALL_DIR}"

# --- Write wrapper script ---
WRAPPER=""
for bin_dir in "/usr/local/bin" "${HOME}/.local/bin"; do
    if [ -d "$bin_dir" ] && [ -w "$bin_dir" ]; then
        WRAPPER="${bin_dir}/${BIN_NAME}"
        break
    fi
done

if [ -z "$WRAPPER" ]; then
    mkdir -p "${HOME}/.local/bin"
    WRAPPER="${HOME}/.local/bin/${BIN_NAME}"
fi

cat > "$WRAPPER" <<EOF
#!/usr/bin/env bash
export PYTHONPATH="${INSTALL_DIR}:\${PYTHONPATH:-}"
exec ${PYTHON} -m studio_console "\$@"
EOF
chmod +x "$WRAPPER"
ok "Command: ${WRAPPER}"

# --- PATH hint if needed ---
if ! echo ":${PATH}:" | grep -q ":$(dirname "$WRAPPER"):"; then
    echo ""
    warn "Add this to your shell profile (~/.bashrc or ~/.zshrc):"
    echo "    export PATH=\"$(dirname "$WRAPPER"):\$PATH\""
fi

echo ""
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}  Studio Console ${LATEST} installed${NC}"
echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo -e "  Run: ${BOLD}studio-console${NC}"
echo ""
echo -e "  License terms: ${INSTALL_DIR}/LICENSE"
echo -e "  Operator obligations: ${INSTALL_DIR}/LEGAL.md"
echo ""
