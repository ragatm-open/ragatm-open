// Crée (idempotent) un lien tests/frontend/node_modules -> frontend/node_modules.
// Nécessaire car la suite de tests vit hors du package frontend/ : la résolution
// des imports bare (vitest, @testing-library/*) doit pouvoir remonter à node_modules.
// node_modules étant gitignoré, ce lien est recréé à chaque `npm test`.
import { existsSync, symlinkSync, lstatSync, rmSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url)); // frontend/scripts
const frontendNodeModules = path.resolve(here, "..", "node_modules");
const testDir = path.resolve(here, "..", "..", "tests", "frontend");
const link = path.join(testDir, "node_modules");

try {
  if (existsSync(link)) {
    const st = lstatSync(link);
    if (st.isSymbolicLink()) {
      process.exit(0); // déjà en place
    }
    rmSync(link, { recursive: true, force: true });
  }
  symlinkSync(frontendNodeModules, link, "junction");
  console.log("[link-test-deps] linked", link, "->", frontendNodeModules);
} catch (e) {
  console.warn("[link-test-deps] warning:", e.message);
}
