#!/bin/bash
# Stamina — Claude MCP Setup
# Run this once on any Mac to connect Claude Desktop to the Stamina database

set -e

echo ""
echo "Setting up Stamina CS Intelligence for Claude Desktop..."
echo ""

# Check Node.js
if ! command -v node &> /dev/null; then
  echo "Node.js not found. Please install from https://nodejs.org and re-run this script."
  exit 1
fi

# Clone / update the MCP server
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

# Install dependencies
echo "Installing dependencies..."
cd "$MCP_DIR" && npm install --quiet

# Create config directory if needed
mkdir -p "$HOME/Library/Application Support/Claude"
CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

# Back up existing config if present
if [ -f "$CONFIG" ]; then
  cp "$CONFIG" "$CONFIG.backup"
  echo "Backed up existing config to claude_desktop_config.json.backup"
fi

# Write the config (uses the absolute path for this user)
cat > "$CONFIG" << ENDOFCONFIG
{
  "mcpServers": {
    "stamina_db": {
      "command": "node",
      "args": [
        "$MCP_DIR/server.js",
        "postgresql://postgres.jgvyeavyffenvuhphejg:5oNcYdBnN4dCsuoS@aws-1-ap-northeast-1.pooler.supabase.com:6543/postgres"
      ]
    }
  }
}
ENDOFCONFIG

echo ""
echo "✓ MCP server installed at $MCP_DIR"
echo "✓ Claude Desktop config written"
echo ""
echo "Next steps:"
echo "  1. Quit Claude Desktop completely (Cmd+Q)"
echo "  2. Reopen Claude Desktop"
echo "  3. Open the Stamina CS Intelligence project"
echo "  4. Ask about any account — Claude will query the live database"
echo ""
