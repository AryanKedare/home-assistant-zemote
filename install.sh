#!/usr/bin/env bash
# install.sh — Install the home-assistant-zemote custom integration
# Usage: bash install.sh [/path/to/homeassistant/config]

set -euo pipefail

REPO_URL="https://github.com/AryanKedare/home-assistant-zemote"
INTEGRATION_NAME="zemote"
CONFIG_DIR="${1:-/config}"
CUSTOM_COMPONENTS_DIR="${CONFIG_DIR}/custom_components"
DEST_DIR="${CUSTOM_COMPONENTS_DIR}/${INTEGRATION_NAME}"
TMP_DIR="$(mktemp -d)"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()    { echo -e "${CYAN}[INFO]${NC}  $*"; }
success() { echo -e "${GREEN}[OK]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ── Dependency check ──────────────────────────────────────────────────────────
for cmd in curl unzip; do
  command -v "$cmd" &>/dev/null || error "'$cmd' is required but not installed."
done

# ── Verify config dir ─────────────────────────────────────────────────────────
if [[ ! -d "$CONFIG_DIR" ]]; then
  error "Config directory '$CONFIG_DIR' does not exist. Pass the correct path as the first argument."
fi

# ── Create custom_components if needed ────────────────────────────────────────
if [[ ! -d "$CUSTOM_COMPONENTS_DIR" ]]; then
  info "Creating '$CUSTOM_COMPONENTS_DIR'..."
  mkdir -p "$CUSTOM_COMPONENTS_DIR"
fi

# ── Download latest ZIP from GitHub ──────────────────────────────────────────
ZIP_URL="${REPO_URL}/archive/refs/heads/ha-zemote.zip"
ZIP_FILE="${TMP_DIR}/zemote.zip"

info "Downloading ${REPO_URL}..."
curl -fsSL "$ZIP_URL" -o "$ZIP_FILE" \
  || error "Download failed. Check your internet connection or verify the repository URL."
success "Download complete."

# ── Extract ───────────────────────────────────────────────────────────────────
info "Extracting archive..."
unzip -q "$ZIP_FILE" -d "$TMP_DIR"
success "Extraction complete."

# ── Locate the integration folder inside the extracted archive ────────────────
# Handles repos where the component lives at:
#   root/custom_components/<name>/  (standard layout)
#   root/<name>/                    (flat layout)
EXTRACTED_ROOT="${TMP_DIR}/home-assistant-zemote-ha-zemote"

if [[ -d "${EXTRACTED_ROOT}/custom_components/${INTEGRATION_NAME}" ]]; then
  SRC_DIR="${EXTRACTED_ROOT}/custom_components/${INTEGRATION_NAME}"
elif [[ -d "${EXTRACTED_ROOT}/${INTEGRATION_NAME}" ]]; then
  SRC_DIR="${EXTRACTED_ROOT}/${INTEGRATION_NAME}"
else
  # Fallback: look for any folder containing manifest.json
  SRC_DIR="$(find "$EXTRACTED_ROOT" -name "manifest.json" -exec dirname {} \; | head -1)"
  [[ -n "$SRC_DIR" ]] || error "Could not locate the integration folder in the repository."
fi

info "Found integration at: ${SRC_DIR}"

# ── Install ───────────────────────────────────────────────────────────────────
if [[ -d "$DEST_DIR" ]]; then
  warn "Existing installation found at '${DEST_DIR}'. Overwriting..."
  rm -rf "$DEST_DIR"
fi

cp -r "$SRC_DIR" "$DEST_DIR"
success "Installed '${INTEGRATION_NAME}' to '${DEST_DIR}'."

# ── Cleanup ───────────────────────────────────────────────────────────────────
rm -rf "$TMP_DIR"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}✔ Installation complete!${NC}"
echo ""
echo "  Next steps:"
echo "  1. Restart Home Assistant."
echo "  2. Go to Settings → Devices & Services → Add Integration."
echo "  3. Search for 'Zemote' and follow the setup flow."
echo ""
echo "  Repository: ${REPO_URL}"
