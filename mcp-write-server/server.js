#!/usr/bin/env node
/**
 * Stamina DB — write-enabled MCP server
 * Usage: node server.js <connection-string>
 */

const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require("@modelcontextprotocol/sdk/types.js");
const { Pool } = require("pg");

const connectionString = process.argv[2];
if (!connectionString) {
  process.stderr.write("Usage: node server.js <postgres-connection-string>\n");
  process.exit(1);
}

const pool = new Pool({ connectionString, ssl: { rejectUnauthorized: false } });

const server = new Server(
  { name: "stamina_db", version: "1.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "query",
      description:
        "Execute SQL against the Stamina Supabase database. Supports SELECT, INSERT, UPDATE, DELETE.",
      inputSchema: {
        type: "object",
        properties: {
          sql: {
            type: "string",
            description: "The SQL query to execute",
          },
        },
        required: ["sql"],
      },
    },
  ],
}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {
  if (request.params.name !== "query") {
    throw new Error(`Unknown tool: ${request.params.name}`);
  }

  const sql = request.params.arguments?.sql;
  if (!sql) {
    throw new Error("Missing required argument: sql");
  }

  try {
    const result = await pool.query(sql);
    const output =
      result.rows && result.rows.length > 0
        ? JSON.stringify(result.rows, null, 2)
        : `Query OK — ${result.rowCount ?? 0} row(s) affected`;

    return {
      content: [{ type: "text", text: output }],
    };
  } catch (err) {
    return {
      content: [{ type: "text", text: `Error: ${err.message}` }],
      isError: true,
    };
  }
});

const transport = new StdioServerTransport();
server.connect(transport).catch((err) => {
  process.stderr.write(`Fatal: ${err.message}\n`);
  process.exit(1);
});
