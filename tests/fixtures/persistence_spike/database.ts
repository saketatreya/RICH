import { PGlite } from "@electric-sql/pglite";
import type { PgDatabase, PgQueryResultHKT } from "drizzle-orm/pg-core";
import { drizzle as drizzlePglite } from "drizzle-orm/pglite";
import { drizzle as drizzlePostgresJs } from "drizzle-orm/postgres-js";
import postgres from "postgres";
import * as schema from "./schema";

/** The database as the domain sees it: one Postgres dialect, whichever engine is underneath. */
export type Database = PgDatabase<PgQueryResultHKT, typeof schema>;

let opened: Promise<Database> | undefined;

/**
 * DATABASE_URL -> Postgres over the wire (preview, production).
 * RICH_DATABASE_DIR -> PGlite inside this process (the network-off gates).
 * Neither -> refuse. There is no third engine and no default.
 */
export function database(): Promise<Database> {
  opened ??= open();
  return opened;
}

async function open(): Promise<Database> {
  const url = process.env.DATABASE_URL;
  if (url) {
    return drizzlePostgresJs(postgres(url, { max: 5 }), { schema });
  }
  const directory = process.env.RICH_DATABASE_DIR;
  if (directory) {
    const client = new PGlite(directory);
    await client.waitReady;
    return drizzlePglite(client, { schema });
  }
  throw new Error("DATABASE_URL or RICH_DATABASE_DIR is required");
}
