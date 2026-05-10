#!/bin/bash
# Stamina — Claude MCP Setup
# Run this once on any Mac to connect Claude Desktop to the Stamina database
# Usage: bash setup-claude-mcp.sh [csm-name]
#   csm-name must match your account_owner value in Stamina exactly (e.g. "Raswant Ravi")
#   Omit for admin access (Rehaan only)

set -e

echo ""
echo "╔══════════════════════════════════════════╗"
echo "║   Stamina CS Intelligence — MCP Setup   ║"
echo "╚══════════════════════════════════════════╝"
echo ""

# ── Get CSM name ───────────────────────────────────────────────────────────────
if [ -n "$1" ]; then
  CSM_NAME="$1"
else
  read -rp "Your name (exactly as it appears in Stamina, or 'admin' for Rehaan): " CSM_NAME
fi

if [ -z "$CSM_NAME" ]; then
  echo "Error: CSM name is required."
  exit 1
fi

echo ""
echo "Setting up for: $CSM_NAME"
echo ""

# ── Check Node.js ──────────────────────────────────────────────────────────────
if ! command -v node &> /dev/null; then
  echo "Node.js not found. Please install from https://nodejs.org and re-run this script."
  exit 1
fi

# ── Download MCP server ────────────────────────────────────────────────────────
STAMINA_DIR="$HOME/stamina"
MCP_DIR="$STAMINA_DIR/mcp-write-server"

if [ -d "$STAMINA_DIR/.git" ]; then
  echo "Updating stamina-sync repo..."
  git -C "$STAMINA_DIR" pull --quiet
else
  echo "Downloading MCP server..."
  mkdir -p "$MCP_DIR"
  curl -fsSL "https://raw.githubusercontent.com/rehaan-ai/stamina-sync/main/mcp-write-server/server.js" -o "$MCP_DIR/server.js"
  curl -fsSL "https://raw.githubusercontent.com/rehaan-ai/stamina-sync/main/mcp-write-server/package.json" -o "$MCP_DIR/package.json"
fi

# ── Install dependencies ───────────────────────────────────────────────────────
echo "Installing dependencies..."
cd "$MCP_DIR" && npm install --quiet

# ── Pick connection string based on admin vs CSM ───────────────────────────────
if [ "$CSM_NAME" = "admin" ]; then
  # Admin uses service role — bypasses RLS entirely
  read -rsp "Enter the admin DB password (ask Rehaan): " DB_PASS
  echo ""
  DB_URL="postgresql://postgres.jgvyeavyffenvuhphejg:${DB_PASS}@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres"
else
  # CSMs use restricted csm_role — RLS enforced by DB
  read -rsp "Enter the DB password (ask Rehaan): " DB_PASS
  echo ""
  DB_URL="postgresql://csm_role.jgvyeavyffenvuhphejg:${DB_PASS}@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres"
fi

# ── Write Claude Desktop config ────────────────────────────────────────────────
mkdir -p "$HOME/Library/Application Support/Claude"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.backup"
  echo "Backed up existing config."
fi

cat > "$CONFIG" << ENDOFCONFIG
{
  "mcpServers": {
    "stamina_db": {
      "command": "node",
      "args": [
        "$MCP_DIR/server.js",
        "$DB_URL",
        "$CSM_NAME"
      ]
    }
  }
}
ENDOFCONFIG

echo ""
echo "✓ MCP server installed at $MCP_DIR"
echo "✓ Scoped to: $CSM_NAME"
echo "✓ Claude Desktop config written"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "Next steps:"
echo "  1. Quit Claude Desktop completely (Cmd+Q)"
echo "  2. Reopen Claude Desktop"
echo "  3. Open the Stamina CS Intelligence project"
echo "  4. Ask about any of your accounts"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
