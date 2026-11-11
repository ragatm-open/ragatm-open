/**
 * Garde-fou : un fichier de route de l'App Router n'exporte QUE ce que Next
 * autorise.
 *
 * Pourquoi ce test existe : `SpacesTab` avait été exporté depuis
 * `app/workspace-admin/page.tsx` pour être testable isolément. C'est interdit —
 * Next génère par route un fichier de garde qui fait
 * `checkFields<Diff<{ default, metadata?, revalidate?, dynamic?, … }, TEntry, ''>>()`
 * et rejette tout export nommé restant. Le défaut n'était visible NULLE PART
 * ailleurs : ni `next dev`, ni `tsc --noEmit`, ni les tests — uniquement la
 * phase de vérification des types de `next build`, qu'aucun workflow ne jouait.
 * Résultat : image de production incontructible pendant six jours.
 *
 * Ce test rend l'erreur détectable en une seconde, sur TOUTES les routes.
 * La CI construit désormais aussi le frontend (`.github/workflows/ci.yml`) :
 * ceinture et bretelles, la ceinture étant beaucoup plus rapide.
 */
import fs from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

const APP_DIR = path.resolve(__dirname, "../../../frontend/src/app");

/**
 * Configuration de segment, acceptée partout. Miroir de
 * `next/dist/build/webpack/plugins/next-types-plugin` (`createTypeGuardFile`).
 */
const SEGMENT_CONFIG = [
  "config",
  "generateStaticParams",
  "revalidate",
  "dynamic",
  "dynamicParams",
  "fetchCache",
  "preferredRegion",
  "runtime",
  "maxDuration",
];

/** Pages, layouts, templates, écrans d'erreur : l'export par défaut + les métadonnées. */
const ALLOWED_COMPONENT = new Set([
  ...SEGMENT_CONFIG,
  "default",
  "metadata",
  "generateMetadata",
  "viewport",
  "generateViewport",
]);

/** Handlers `route.ts` : une fonction par verbe HTTP, et pas d'export par défaut. */
const ALLOWED_ROUTE = new Set([
  ...SEGMENT_CONFIG,
  "GET",
  "HEAD",
  "OPTIONS",
  "POST",
  "PUT",
  "DELETE",
  "PATCH",
]);

/** Fichiers de route : `page`, `layout`, `route`, `template`, `error`… */
const ROUTE_FILE = /^(page|layout|template|route|error|loading|not-found|global-error)\.tsx?$/;

function allowedFor(file: string): Set<string> {
  return /(^|\/)route\.tsx?$/.test(file) ? ALLOWED_ROUTE : ALLOWED_COMPONENT;
}

function routeFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    // Les dossiers préfixés `_` sont hors routage : les exports nommés y sont libres.
    if (entry.isDirectory()) {
      if (!entry.name.startsWith("_")) routeFiles(full, out);
    } else if (ROUTE_FILE.test(entry.name)) {
      out.push(full);
    }
  }
  return out;
}

/**
 * Noms exportés d'un module, hors export par défaut. Analyse textuelle
 * volontairement simple : elle couvre les formes réellement écrites dans ce
 * dépôt (`export function X`, `export const X`, `export type X`,
 * `export { X, Y }`), et un faux positif se corrige en déplaçant le symbole —
 * ce qui est précisément le comportement voulu.
 */
function namedExports(source: string): string[] {
  const names: string[] = [];

  for (const m of source.matchAll(
    /^export\s+(?:async\s+)?(?:function|const|let|var|class|type|interface|enum)\s+([A-Za-z0-9_$]+)/gm
  )) {
    names.push(m[1]);
  }

  for (const m of source.matchAll(/^export\s*\{([^}]*)\}/gm)) {
    for (const raw of m[1].split(",")) {
      const piece = raw.trim();
      if (!piece) continue;
      // `X as Y` → c'est `Y` qui est exporté ; `X as default` reste légal.
      const exported = (piece.split(/\s+as\s+/).pop() || "").replace(/^type\s+/, "").trim();
      if (exported) names.push(exported);
    }
  }

  return names;
}

describe("App Router — exports des fichiers de route", () => {
  const files = routeFiles(APP_DIR);

  it("trouve bien les fichiers de route à contrôler", () => {
    expect(files.length).toBeGreaterThan(5);
  });

  it.each(files.map((f) => [path.relative(APP_DIR, f), f]))(
    "%s n'exporte que des champs autorisés",
    (_label, file) => {
      const allowed = allowedFor(file);
      const illegal = namedExports(fs.readFileSync(file, "utf8")).filter((n) => !allowed.has(n));
      expect(
        illegal,
        `${path.relative(APP_DIR, file)} exporte ${illegal.join(", ")} : ` +
          "un fichier de route n'accepte que l'export par défaut et la configuration " +
          "de segment. Déplacez le symbole dans un dossier `_components/` ou `_lib/` " +
          "(hors routage), sinon `next build` échouera."
      ).toEqual([]);
    }
  );
});
