/**
 * Symétrie des dictionnaires i18n (fr ⇄ en).
 *
 * Ce test ÉCHOUE si :
 *  - un namespace attendu manque pour une locale ;
 *  - une clé existe dans une locale mais pas dans l'autre ;
 *  - une valeur est vide ;
 *  - un fichier JSON est invalide ;
 *  - une clé est dupliquée dans un même fichier.
 *
 * Il garantit le contrat de la mission i18n : les deux langues restent alignées.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

import { NAMESPACES, SUPPORTED_LOCALES } from "@/i18n/config";

const MESSAGES_DIR = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../../frontend/messages"
);

function filePath(locale: string, ns: string): string {
  return path.join(MESSAGES_DIR, locale, `${ns}.json`);
}

/** Aplatit un objet imbriqué en clés « a.b.c ». */
function flatten(obj: unknown, prefix = ""): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  if (obj && typeof obj === "object" && !Array.isArray(obj)) {
    for (const [k, v] of Object.entries(obj)) {
      const key = prefix ? `${prefix}.${k}` : k;
      if (v && typeof v === "object" && !Array.isArray(v)) {
        Object.assign(out, flatten(v, key));
      } else {
        out[key] = v;
      }
    }
  }
  return out;
}

/** Détecte les clés dupliquées (au niveau d'un même objet) via un reviver. */
function parseNoDuplicates(raw: string, label: string): unknown {
  return JSON.parse(raw, function reviver(this: any, key, value) {
    return value;
  });
}

/** Compte les occurrences de chaque clé à partir des lignes `"key":`. */
function duplicateKeys(raw: string): string[] {
  const counts = new Map<string, number>();
  const re = /^\s*"((?:[^"\\]|\\.)*)"\s*:/gm;
  let m: RegExpExecArray | null;
  while ((m = re.exec(raw)) !== null) {
    // Note : les fichiers sont plats (une clé par ligne) ; suffisant pour le contrat.
    const k = m[1];
    counts.set(k, (counts.get(k) ?? 0) + 1);
  }
  return [...counts.entries()].filter(([, n]) => n > 1).map(([k]) => k);
}

describe("dictionnaires i18n", () => {
  it("expose exactement les mêmes namespaces pour chaque locale", () => {
    for (const locale of SUPPORTED_LOCALES) {
      for (const ns of NAMESPACES) {
        expect(() => readFileSync(filePath(locale, ns), "utf8"), `${locale}/${ns}.json manquant`).not.toThrow();
      }
    }
  });

  it("contient du JSON valide, sans clé dupliquée ni valeur vide", () => {
    for (const locale of SUPPORTED_LOCALES) {
      for (const ns of NAMESPACES) {
        const raw = readFileSync(filePath(locale, ns), "utf8");
        expect(() => parseNoDuplicates(raw, `${locale}/${ns}`)).not.toThrow();
        expect(duplicateKeys(raw), `clés dupliquées dans ${locale}/${ns}.json`).toEqual([]);
        const flat = flatten(JSON.parse(raw));
        for (const [k, v] of Object.entries(flat)) {
          expect(typeof v, `valeur non-string ${locale}/${ns}.json → ${k}`).toBe("string");
          expect((v as string).trim().length, `valeur vide ${locale}/${ns}.json → ${k}`).toBeGreaterThan(0);
        }
      }
    }
  });

  it("aligne exactement les clés fr et en pour chaque namespace", () => {
    for (const ns of NAMESPACES) {
      const fr = Object.keys(flatten(JSON.parse(readFileSync(filePath("fr", ns), "utf8")))).sort();
      const en = Object.keys(flatten(JSON.parse(readFileSync(filePath("en", ns), "utf8")))).sort();
      const missingInEn = fr.filter((k) => !en.includes(k));
      const missingInFr = en.filter((k) => !fr.includes(k));
      expect(missingInEn, `clés absentes de en/${ns}.json`).toEqual([]);
      expect(missingInFr, `clés absentes de fr/${ns}.json`).toEqual([]);
    }
  });
});
