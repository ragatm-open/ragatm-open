"use client";

/**
 * Choix du mode d'analyse, sous le message de clarification : deux vrais boutons,
 * là où il fallait auparavant RECOPIER « réponse rapide » ou « analyse complète ».
 *
 * Le composant ne connaît que des identifiants d'action stables — jamais le
 * Markdown affiché, jamais un libellé porteur de sens. Il n'appelle rien lui-même :
 * il remonte le choix, et le serveur reste seul juge de ce qu'il en fait.
 *
 * Volontairement SPÉCIFIQUE : deux boutons ne justifient pas un système générique
 * d'actions de message. Le jour où une troisième action apparaîtra, on généralisera.
 */
import { useTranslations } from "next-intl";
import { CORPUS_CHOICE_LABEL_KEYS, type CorpusChoice } from "../_lib/types";

export function CorpusScopeChoice({
  onChoose,
  globalAnalysisAvailable,
  pending = null,
  disabled = false,
}: {
  onChoose: (choice: CorpusChoice) => void;
  /** Sans sujet identifiable, le moteur n'a rien à chercher : pas de bouton. */
  globalAnalysisAvailable: boolean;
  /** Choix en cours d'envoi, ou `null`. */
  pending?: CorpusChoice | null;
  disabled?: boolean;
}) {
  const t = useTranslations("chat");
  const busy = pending !== null;

  const button = (choice: CorpusChoice, primary: boolean) => {
    const inFlight = pending === choice;
    return (
      <button
        type="button"
        onClick={() => onChoose(choice)}
        // Désactivés dès le premier clic : le double clic ne peut pas partir deux fois.
        // Le backend refuse de toute façon un second choix — ceci n'est que la
        // première des deux barrières.
        disabled={disabled || busy}
        aria-busy={inFlight}
        style={{
          flex: "1 1 12rem",       // se replie et s'empile sous ~390 px
          minHeight: 44,           // zone tactile confortable
          padding: "10px 16px",
          borderRadius: 10,
          fontSize: 14,
          fontWeight: primary ? 700 : 500,
          cursor: disabled || busy ? "not-allowed" : "pointer",
          opacity: disabled || busy ? 0.6 : 1,
          // L'état n'est jamais porté par la seule couleur : le libellé change aussi.
          color: primary ? "#fff" : "var(--ink)",
          background: primary ? "var(--accent, #0E7C86)" : "var(--surface, #fff)",
          border: primary ? "1px solid var(--accent, #0E7C86)" : "1px solid var(--line, #ddd)",
        }}
      >
        {inFlight ? t("launching") : t(CORPUS_CHOICE_LABEL_KEYS[choice])}
      </button>
    );
  };

  return (
    <div
      role="group"
      aria-label={t("chooseAnalysisMode")}
      data-testid="corpus-scope-choice"
      style={{
        display: "flex",
        flexWrap: "wrap",          // mobile : les boutons passent à la ligne
        gap: 10,
        margin: "12px 0 4px",
      }}
    >
      {button("quick_response", false)}
      {globalAnalysisAvailable && button("global_analysis", true)}
    </div>
  );
}
