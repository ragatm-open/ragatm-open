#!/usr/bin/env node
/**
 * Audit des chaînes visibles codées en dur (mission i18n, §17).
 *
 * Détecte, dans le JSX/TSX, des chaînes de caractères VISIBLES probablement
 * françaises qui ne passent pas par le système de traduction (`t(...)` /
 * `getTranslations`). Le contrôle est volontairement CIBLÉ (heuristique), pas
 * naïf : il ne bloque pas toutes les chaînes TypeScript.
 *
 * Signaux retenus :
 *   - texte JSX entre balises contenant des lettres accentuées françaises ;
 *   - attributs visibles (placeholder, title, aria-label, alt, label) dont la
 *     valeur littérale contient des lettres accentuées.
 *
 * Exceptions documentées (ignorées) :
 *   - marque et noms propres (RAGatm, RagSpace, RagATM_Core, AI Gateway) ;
 *   - identifiants techniques ALL_CAPS_SNAKE (AI_GATEWAY_ENABLED, …) ;
 *   - contenu déjà dans un appel t(...) / getTranslations ;
 *   - fichiers de dictionnaires, tests, et fichiers _lib de logique pure.
 *
 * Usage :
 *   node scripts/i18n-audit.mjs [--changed] [--strict]
 *     --changed : n'analyse que les fichiers modifiés (git diff) ;
 *     --strict  : code de sortie 1 si des chaînes suspectes subsistent.
 *
 * Par défaut : rapport informatif, code de sortie 0.
 */
import { readFileSync, readdirSync } from "node:fs";
import { execSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import path from "node:path";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const args = new Set(process.argv.slice(2));
const CHANGED = args.has("--changed");
const STRICT = args.has("--strict");

// Lettres accentuées françaises = signal fort de texte visible non traduit.
const FRENCH = /[àâäçéèêëîïôöùûüœÀÂÄÇÉÈÊËÎÏÔÖÙÛÜŒ]/;
const ALLOWED_BRANDS = ["RAGatm", "RagSpace", "RagATM_Core", "AI Gateway"];
const VISIBLE_ATTRS = ["placeholder", "title", "aria-label", "alt", "label"];

function walkTsx(dir, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === "node_modules" || entry.name.startsWith(".")) continue;
      walkTsx(full, out);
    } else if (entry.isFile() && full.endsWith(".tsx")) {
      out.push(full);
    }
  }
  return out;
}

function listFiles() {
  if (CHANGED) {
    const out = execSync("git diff --name-only --diff-filter=ACMR HEAD", { cwd: ROOT }).toString();
    return out
      .split("\n")
      .map((f) => f.trim())
      .filter((f) => /\.(tsx)$/.test(f))
      .map((f) => path.resolve(ROOT, "..", f))
      .filter((f) => f.startsWith(path.join(ROOT, "src")));
  }
  return walkTsx(path.join(ROOT, "src"));
}

function isException(text) {
  const trimmed = text.trim();
  if (!trimmed) return true;
  if (ALLOWED_BRANDS.some((b) => trimmed.includes(b))) return true;
  if (/^[A-Z0-9_]+$/.test(trimmed)) return true; // identifiant technique
  return false;
}

function auditFile(file) {
  const findings = [];
  let src;
  try {
    src = readFileSync(file, "utf8");
  } catch {
    return findings;
  }
  const lines = src.split("\n");
  lines.forEach((line, i) => {
    const trimmed = line.trim();
    if (trimmed.startsWith("//") || trimmed.startsWith("*")) return;
    // Ignore les lignes déjà branchées sur la traduction.
    if (/\bt\(|getTranslations|useTranslations/.test(line)) return;

    // 1) Attributs visibles avec littéral accentué.
    for (const attr of VISIBLE_ATTRS) {
      const re = new RegExp(`${attr}=\\{?["']([^"']*${FRENCH.source}[^"']*)["']`);
      const m = line.match(re);
      if (m && !isException(m[1])) {
        findings.push({ line: i + 1, kind: attr, text: m[1] });
      }
    }

    // 2) Texte JSX entre balises : >...accent...<
    const jsxText = line.match(/>\s*([^<>{}]*[àâäçéèêëîïôöùûüœÀ-Ü][^<>{}]*)</);
    if (jsxText) {
      const text = jsxText[1].trim();
      if (text.length > 1 && !isException(text) && FRENCH.test(text)) {
        findings.push({ line: i + 1, kind: "jsx-text", text });
      }
    }
  });
  return findings;
}

const files = listFiles();
let total = 0;
const report = [];
for (const file of files) {
  const findings = auditFile(file);
  if (findings.length) {
    total += findings.length;
    report.push({ file: path.relative(path.join(ROOT, ".."), file), findings });
  }
}

if (total === 0) {
  console.log("✅ Aucune chaîne visible française non traduite détectée.");
  process.exit(0);
}

console.log(`⚠  ${total} chaîne(s) visible(s) potentiellement non traduite(s) :\n`);
for (const { file, findings } of report) {
  console.log(`  ${file}`);
  for (const f of findings) {
    console.log(`    L${f.line} [${f.kind}] ${f.text.slice(0, 80)}`);
  }
}
console.log(
  "\nCes signaux sont heuristiques (français accentué). Externalisez-les via t(...) " +
    "ou documentez l'exception."
);
process.exit(STRICT ? 1 : 0);
