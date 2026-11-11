import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "node:path";

// La suite de tests vit hors du dossier frontend (../tests/frontend),
// mais doit résoudre l'alias "@/..." vers frontend/src et utiliser ce node_modules.
export default defineConfig({
  plugins: [react({ babel: { plugins: ["styled-jsx/babel"] } })],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "src"),
    },
  },
  // tests/ vit hors de frontend/ : autoriser Vite à servir le parent
  server: {
    fs: { allow: [path.resolve(__dirname, "..")] },
  },
  test: {
    globals: true,
    environment: "jsdom",
    setupFiles: [path.resolve(__dirname, "../tests/frontend/setup.ts")],
    // Vitest 4 : avec un `include` absolu hors de `root`, l'exclusion par défaut de
    // `node_modules` ne s'appliquait plus au lien `tests/frontend/node_modules` (posé par
    // scripts/link-test-deps.mjs) et les tests internes des paquets tiers étaient collectés.
    // Le répertoire de la suite est donc déclaré via `dir`, avec des motifs relatifs.
    dir: path.resolve(__dirname, "../tests/frontend"),
    include: ["**/*.test.{ts,tsx}"],
    exclude: ["**/node_modules/**"],
    css: false,
  },
});
