#!/usr/bin/env node
/**
 * Stamina DB — write-enabled, CSM-scoped MCP server
 * Usage: node server.js <connection-string> <csm-name>
 *
 * csm-name must match account_owner values in the customers table exactly.
 * Use "admin" to bypass all filters (Rehaan only).
 */

const { Server } = require("@modelcontextprotocol/sdk/server/index.js");
const { StdioServerTransport } = require("@modelcontextprotocol/sdk/server/stdio.js");
const {
  CallToolRequestSchema,
  ListToolsRequestSchema,
} = require("@modelcontextprotocol/sdk/types.js");
const { Pool } = require("pg");

const connectionString = process.argv[2];
const csmName = process.argv[3] || "admin";

if (!connectionString) {
  process.stderr.write("Usage: node server.js <postgres-connection-string> <csm-name>\n");
  process.exit(1);
}

const pool = new Pool({ connectionString, ssl: { rejectUnauthorized: false } });

const server = new Server(
  { name: "stamina_db", version: "2.0.0" },
  { capabilities: { tools: {} } }
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "query",
      description:
        "Execute SQL against the Stamina Supabase database. Supports SELECT, INSERT, UPDATE, DELETE. Row-level security is automatically applied — you only see accounts assigned to you.",
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

  const client = await pool.connect();
  try {
    // Scope every query to this CSM using a transaction-local setting.
    // RLS policies read app.csm_name to filter rows.
    // set_config(..., true) = local to this transaction only — works with pooler.
    await client.query("BEGIN");
    await client.query("SELECT set_config('app.csm_name', $1, true)", [csmName]);

    const result = await client.query(sql);
    await client.query("COMMIT");

    const output =
      result.rows && result.rows.length > 0
        ? JSON.stringify(result.rows, null, 2)
        : `Query OK — ${result.rowCount ?? 0} row(s) affected`;

    return {
      content: [{ type: "text", text: output }],
    };
  } catch (err) {
    await client.query("ROLLBACK").catch(() => {});
    return {
      content: [{ type: "text", text: `Error: ${err.message}` }],
      isError: true,
    };
  } finally {
    client.release();
  }
});

const transport = new StdioServerTransport();
server.connect(transport).catch((err) => {
  process.stderr.write(`Fatal: ${err.message}\n`);
  process.exit(1);
});
