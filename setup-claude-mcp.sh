#!/bin/bash
# Stamina — Claude MCP Setup
# Run this once on any Mac to connect Claude Desktop to the Stamina database

echo ""
echo "Setting up Stamina MCP for Claude Desktop..."
echo ""

# Check Node.js
if ! command -v npx &> /dev/null; then
  echo "Node.js not found. Installing..."
  brew install node
fi

# Create config directory if needed
mkdir -p "$HOME/Library/Application Support/Claude"

CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

# If config exists, merge — otherwise create fresh
if [ -f "$CONFIG" ]; then
  # Back up existing config
  cp "$CONFIG" "$CONFIG.backup"
  echo "Backed up existing config to claude_desktop_config.json.backup"
fi

# Write the config
cat > "$CONFIG" << 'ENDOFCONFIG'
{
  "mcpServers": {
    "supabase": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-postgres",
        "postgresql://postgres:5oNcYdBnN4dCsuoS@db.jgvyeavyffenvuhphejg.supabase.co:5432/postgres"
      ]
    }
  }
}
ENDOFCONFIG

echo "✓ Config written"
echo ""
echo "Done! Now:"
echo "  1. Quit Claude Desktop completely (Cmd+Q)"
echo "  2. Reopen Claude Desktop"
echo "  3. Ask Claude anything about your accounts"
echo ""
