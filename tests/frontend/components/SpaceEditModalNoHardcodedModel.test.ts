/**
 * Garde STRUCTURELLE : le formulaire d'espace ne contient aucun nom de modèle
 * codé en dur dans son code actif.
 *
 * Pourquoi une garde structurelle en plus des tests de comportement : le défaut
 * corrigé ici n'était pas une erreur de logique mais une DUPLICATION. Le composant
 * portait `llm_model: "llama3.1:8b"`, préremplissait le champ, et envoyait donc
 * cette valeur explicitement — le backend la recevait comme un choix utilisateur
 * et l'appliquait fidèlement. En production, la configuration visait
 * `qwen3:30b-instruct` et c'est `llama3.1:8b` qui répondait.
 *
 * Un test de comportement passe encore si quelqu'un réintroduit un littéral en
 * REPLI (« au cas où l'API échoue ») : le chemin nominal reste vert. Seule une
 * garde sur le source attrape ce cas.
 *
 * Les COMMENTAIRES sont exclus de l'analyse : ils citent volontairement les noms
 * de modèles pour expliquer l'historique, et une garde qui interdirait d'en parler
 * rendrait le code moins compréhensible sans rien protéger de plus.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { describe, expect, it } from "vitest";

const COMPONENT = resolve(
  __dirname,
  "../../../frontend/src/app/workspace-admin/_components/SpaceEditModal.tsx",
);

/** Retire commentaires de bloc puis de ligne. */
function activeCode(source: string): string {
  const noBlock = source.replace(/\/\*[\s\S]*?\*\//g, "");
  return noBlock
    .split("\n")
    .filter((line) => !line.trim().startsWith("//"))
    .map((line) => line.replace(/\/\/.*$/, ""))
    .join("\n");
}

/** Littéraux chaîne du code actif. */
function stringLiterals(code: string): string[] {
  return [...code.matchAll(/"([^"\\\n]*)"/g)].map((m) => m[1]);
}

describe("SpaceEditModal — aucun modèle codé en dur", () => {
  const code = activeCode(readFileSync(COMPONENT, "utf8"));
  const literals = stringLiterals(code);

  it("ne contient aucun littéral de la forme `nom:tag` (convention Ollama)", () => {
    // `llama3.1:8b`, `qwen3:30b-instruct`, `mistral-small3.2:24b`, `gemma3:27b`…
    const modelLike = literals.filter((s) => /^[A-Za-z0-9][A-Za-z0-9._\-]*:[A-Za-z0-9._\-]+$/.test(s));
    expect(modelLike).toEqual([]);
  });

  it("ne cite aucun nom de modèle connu, même sans deux-points", () => {
    // Couvre aussi les noms de modèles sans `:` (profils d'embeddings, modèles externes historiques).
    const KNOWN = [
      "llama3.1:8b",
      "qwen3:30b-instruct",
      "mistral-small3.2:24b",
      "gemma3:27b",
      "gpt-4.1-mini",
      "gpt-4o-mini",
      "text-embedding-3-small",
      "nomic-embed-text",
    ];
    expect(literals.filter((s) => KNOWN.includes(s))).toEqual([]);
  });

  it("lit bien le bon fichier (la garde ne doit pas passer sur un fichier vide)", () => {
    // Sans cette vérification, un chemin cassé rendrait les deux tests ci-dessus
    // trivialement verts — la garde serait silencieusement désarmée.
    expect(code).toContain("SpaceEditModal");
    expect(code).toContain("ragspace_model_policy");
    expect(literals.length).toBeGreaterThan(10);
  });
});
