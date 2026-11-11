import "@testing-library/jest-dom/vitest";
import { afterEach, vi } from "vitest";
import { cleanup } from "@testing-library/react";

// ─── i18n dans les tests ─────────────────────────────────────────────────────
// Les composants migrés appellent `useTranslations` (next-intl), qui exige
// normalement un `NextIntlClientProvider`. Plutôt que d'envelopper chaque `render`
// des ~34 suites de composants, on adosse `useTranslations`/`useFormatter` aux
// VRAIS dictionnaires français via `createTranslator` (use-intl), SANS contexte
// React. Les composants rendent donc exactement le français de production.
//
// `useLocale` et `NextIntlClientProvider` restent RÉELS : les tests qui
// fournissent explicitement un provider (ex. LanguageSelector) gardent leur
// comportement, y compris la locale du provider.
vi.mock("next-intl", async (importActual) => {
  const actual = (await importActual()) as Record<string, unknown>;
  const { createTranslator, createFormatter } = await import("use-intl");
  const { readFileSync } = await import("node:fs");
  const { fileURLToPath } = await import("node:url");
  const nodePath = (await import("node:path")).default;

  const dir = nodePath.resolve(nodePath.dirname(fileURLToPath(import.meta.url)), "../../frontend/messages/fr");
  const NAMESPACES = [
    "common", "navigation", "auth", "login", "passwordReset", "invitations", "home",
    "chat", "documents", "documentViewer", "corpora", "workspaces", "workspaceAdmin",
    "platformAdmin", "adminCore", "statuses", "errors", "accessibility", "answerLanguage", "navigationSearch",
    "documentCapabilities",
  ];
  const messages: Record<string, unknown> = {};
  for (const ns of NAMESPACES) {
    try {
      messages[ns] = JSON.parse(readFileSync(nodePath.join(dir, `${ns}.json`), "utf8"));
    } catch {
      messages[ns] = {};
    }
  }

  // Traducteur tolérant : une clé manquante renvoie la clé (jamais d'exception),
  // pour ne pas faire planter le rendu d'un composant sur un cas limite de test.
  const onError = () => {};
  const getMessageFallback = ({ key }: { key: string }) => key;

  // IMPORTANT : next-intl renvoie un `t` STABLE entre les rendus (mémoïsé). On
  // reproduit cette stabilité en cachant un traducteur par namespace. Sans cela,
  // un nouveau `t` à chaque rendu invalide les `useCallback([t])`/`useEffect`
  // dépendants et provoque des boucles de rendu infinies (artefact de test, pas
  // un bug produit).
  const translatorCache = new Map<string, unknown>();
  const formatter = createFormatter({ locale: "fr" });

  return {
    ...actual,
    // Locale de test contrôlable (défaut fr). Les tests qui veulent l'anglais
    // posent `globalThis.__TEST_LOCALE__ = "en"`.
    useLocale: () => (globalThis as any).__TEST_LOCALE__ ?? "fr",
    useTranslations: (namespace?: string) => {
      const key = namespace ?? "";
      if (!translatorCache.has(key)) {
        translatorCache.set(
          key,
          createTranslator({
            locale: "fr",
            messages: messages as any,
            namespace: namespace as any,
            onError,
            getMessageFallback,
          })
        );
      }
      return translatorCache.get(key) as ReturnType<typeof createTranslator>;
    },
    useFormatter: () => formatter,
  };
});

// Nettoyage du DOM RTL entre chaque test
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  try {
    window.localStorage.clear();
  } catch {
    /* noop */
  }
});

// jsdom n'implémente pas scrollIntoView (utilisé par des composants UI)
if (typeof Element !== "undefined" && !Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = vi.fn();
}
