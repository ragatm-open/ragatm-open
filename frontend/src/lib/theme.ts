// RAGatm — accents thématiques (par espace) + application au DOM.
// Permet de teinter l'UI selon l'espace courant, comme dans la maquette.

export type AccentTokens = { soft: string; ink: string; glow: string; dot: string };

export const RG_ACCENTS: Record<string, AccentTokens> = {
  "#C2562F": { soft: "#F8E7DD", ink: "#9A3D17", glow: "rgba(194,86,47,.16)", dot: "#EBD9CC" }, // terracotta (RH)
  "#0E7C86": { soft: "#DCF0F1", ink: "#0A5A62", glow: "rgba(14,124,134,.16)", dot: "#CDE7E9" }, // teal (Juridique)
  "#5B53D9": { soft: "#E8E6FB", ink: "#3E37A8", glow: "rgba(91,83,217,.16)", dot: "#DAD7F4" }, // indigo (Procédures)
  "#1F8A5B": { soft: "#DEF1E7", ink: "#15663F", glow: "rgba(31,138,91,.16)", dot: "#CFE9DA" }, // green (Support)
  "#2A6FDB": { soft: "#DEEAFB", ink: "#1C4FA8", glow: "rgba(42,111,219,.16)", dot: "#CFE0F6" },
};

const ACCENT_CYCLE = Object.keys(RG_ACCENTS);

/** Couleur stable dérivée d'un identifiant (pour colorer un espace sans champ couleur en base). */
export function accentForId(id: string | number | null | undefined): string {
  const s = String(id ?? "");
  let h = 0;
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
  return ACCENT_CYCLE[h % ACCENT_CYCLE.length];
}

/** Applique une couleur d'accent (+ ses dérivés) sur :root. */
export function applyAccent(accent: string) {
  const a = RG_ACCENTS[accent] || RG_ACCENTS["#C2562F"];
  const r = document.documentElement.style;
  r.setProperty("--accent", accent);
  r.setProperty("--accent-soft", a.soft);
  r.setProperty("--accent-ink", a.ink);
  r.setProperty("--accent-glow", a.glow);
  r.setProperty("--dot", a.dot);
}
