// RICH persistence probe -- the spike's fixture for tests/test_persistence_spike_live.py.
//
// Trusted, never generated: the engine runs it in the same network-off sandbox as
// the gates to observe what the database directory holds. It opens the directory
// with the exact engine the data package was installed with -- pnpm exposes
// @electric-sql/pglite nowhere else, so resolution goes through packages/db on
// purpose -- applies the package's migrations with the one algorithm preview.py
// already runs on Neon (name order, `--> statement-breakpoint`, a journal of
// filename and sha256), optionally exercises a write, and prints one line:
//
//   RICH_DATABASE_PROBE {"schema_version":"rich.database-probe/v1", ...}
//
//   node .rich/verify-database.mjs              # migrate if needed, count rows
//   node .rich/verify-database.mjs --exercise   # ...then insert a row, read it back
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { readdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const started = process.hrtime.bigint();
const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const dataPackage = resolve(root, "packages", "db");
const directory = process.env.RICH_DATABASE_DIR;
if (!directory) throw new Error("RICH_DATABASE_DIR is required");
const exercise = process.argv.includes("--exercise");

const engineEntry = createRequire(
  pathToFileURL(join(dataPackage, "package.json")),
).resolve("@electric-sql/pglite");
const engineModule = await import(pathToFileURL(engineEntry).href);
const PGlite = engineModule.PGlite ?? engineModule.default?.PGlite;
if (typeof PGlite !== "function") {
  throw new Error(`@electric-sql/pglite resolved to ${engineEntry} but exports no PGlite`);
}
const engineVersion = JSON.parse(
  await readFile(join(dirname(engineEntry), "..", "package.json"), "utf8"),
).version;

const db = new PGlite(directory);
await db.waitReady;
try {
  const serverVersion = (await db.query("SELECT version() AS version")).rows[0].version;
  const migrations = await migrate(db, join(dataPackage, "migrations"));
  let exercised = null;
  if (exercise) {
    const inserted = (
      await db.query("INSERT INTO todos (title) VALUES ($1) RETURNING id, title", [
        "probe row",
      ])
    ).rows[0];
    const read = (
      await db.query("SELECT id, title FROM todos WHERE id = $1", [inserted.id])
    ).rows[0];
    exercised = {
      inserted,
      read_back: read !== undefined && read.title === inserted.title,
    };
  }
  const tables = {};
  const names = await db.query(
    "SELECT table_name FROM information_schema.tables " +
      "WHERE table_schema = 'public' AND table_name <> '__rich_migrations' " +
      "ORDER BY table_name",
  );
  for (const { table_name: name } of names.rows) {
    const counted = await db.query(`SELECT count(*)::int AS n FROM "${name}"`);
    tables[name] = Number(counted.rows[0].n);
  }
  const report = {
    schema_version: "rich.database-probe/v1",
    engine: {
      name: "pglite",
      version: engineVersion,
      entry: engineEntry,
      server_version: serverVersion,
    },
    directory,
    migrations,
    tables,
    exercised,
    memory: memory(),
    duration_ms: Number((process.hrtime.bigint() - started) / 1000000n),
  };
  process.stdout.write(`RICH_DATABASE_PROBE ${JSON.stringify(report)}\n`);
} finally {
  await db.close();
}

async function migrate(db, folder) {
  await db.exec(
    "CREATE TABLE IF NOT EXISTS public.__rich_migrations (" +
      "filename text PRIMARY KEY, sha256 text NOT NULL, " +
      "applied_at timestamptz NOT NULL DEFAULT now())",
  );
  const files = (await readdir(folder)).filter((name) => name.endsWith(".sql")).sort();
  const journal = [];
  for (const filename of files) {
    const payload = await readFile(join(folder, filename));
    const sha256 = createHash("sha256").update(payload).digest("hex");
    const existing = (
      await db.query("SELECT sha256 FROM public.__rich_migrations WHERE filename = $1", [
        filename,
      ])
    ).rows[0];
    if (existing) {
      if (existing.sha256 !== sha256) {
        throw new Error(`applied migration ${filename} changed content`);
      }
      journal.push({ file: filename, sha256, applied: false });
      continue;
    }
    const statements = payload
      .toString("utf8")
      .split("--> statement-breakpoint")
      .map((statement) => statement.trim())
      .filter(Boolean);
    if (statements.length === 0 || statements.length > 512) {
      throw new Error(`migration ${filename} has invalid statement boundaries`);
    }
    await db.transaction(async (tx) => {
      for (const statement of statements) await tx.exec(statement);
      await tx.query(
        "INSERT INTO public.__rich_migrations (filename, sha256) VALUES ($1, $2)",
        [filename, sha256],
      );
    });
    journal.push({ file: filename, sha256, applied: true });
  }
  return journal;
}

function memory() {
  // What the sandbox's RLIMIT_AS actually sees: this process's peak virtual
  // size, read from inside, beside V8's own accounting.
  const usage = process.memoryUsage();
  const status = {};
  try {
    for (const line of readFileSync("/proc/self/status", "utf8").split("\n")) {
      const match = /^(VmPeak|VmSize|VmHWM|VmRSS):\s+(\d+) kB$/.exec(line);
      if (match) status[`${match[1].toLowerCase()}_kb`] = Number(match[2]);
    }
  } catch {
    // procfs is optional evidence; the line is still worth printing without it.
  }
  return {
    rss: usage.rss,
    heap_total: usage.heapTotal,
    external: usage.external,
    array_buffers: usage.arrayBuffers,
    ...status,
  };
}
