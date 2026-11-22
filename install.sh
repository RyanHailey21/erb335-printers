#!/usr/bin/env bash
# One-line installer for UTA ERB 335 Printers Skill + CLI (macOS / Linux)
# Usage: curl -fsSL https://raw.githubusercontent.com/RyanHailey21/erb335-printers/main/install.sh | bash
set -euo pipefail

REPO_RAW="https://raw.githubusercontent.com/RyanHailey21/erb335-printers/main/skills/erb335-printers"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

mkdir -p "$TMP_DIR/scripts"
echo "Downloading erb335-printers skill & CLI..."
curl -fsSL "$REPO_RAW/SKILL.md" -o "$TMP_DIR/SKILL.md"
curl -fsSL "$REPO_RAW/scripts/printer_cli.py" -o "$TMP_DIR/scripts/printer_cli.py"
chmod +x "$TMP_DIR/scripts/printer_cli.py"

SKILL_ROOTS=(
  "$HOME/.agents/skills/erb335-printers"
  "$HOME/.claude/skills/erb335-printers"
  "$HOME/.codex/skills/erb335-printers"
  "$HOME/.gemini/config/skills/erb335-printers"
)

for dest in "${SKILL_ROOTS[@]}"; do
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  cp -R "$TMP_DIR" "$dest"
  echo "  Installed skill -> $dest"
done

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"
cat > "$BIN_DIR/erb335-printers" << 'EOF'
#!/usr/bin/env bash
exec python3 "$HOME/.agents/skills/erb335-printers/scripts/printer_cli.py" "$@"
EOF
chmod +x "$BIN_DIR/erb335-printers"
echo "  Installed CLI   -> $BIN_DIR/erb335-printers"

case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo "Note: add '$BIN_DIR' to your PATH (e.g. export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
esac

echo "Done! Run 'erb335-printers status' to verify."
