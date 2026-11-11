/**
 * Panneau droit de /login : scène illustrative du pipeline RAG.
 *
 * Ce que ces tests verrouillent : le contenu pédagogique (les étapes du pipeline et la
 * réponse sourcée), l'ordre « recherche → reranking → LLM → réponse » (le LLM n'est
 * jamais présenté comme une source), l'absence de tout appel réseau ou timer, et
 * l'accessibilité : scène masquée aux lecteurs d'écran, description textuelle à la
 * place, aucun élément focalisable.
 */
import { readFileSync } from "node:fs";
import nodePath from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import PremiumRagPipelineHero from "@/components/login/PremiumRagPipelineHero";

const here = nodePath.dirname(fileURLToPath(import.meta.url));
const dict = (lang: string) =>
  JSON.parse(readFileSync(nodePath.resolve(here, `../../../frontend/messages/${lang}/login.json`), "utf8"));
const fr = dict("fr");
const en = dict("en");

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("PremiumRagPipelineHero", () => {
  it("affiche le titre, le sous-titre et toutes les étapes du pipeline", () => {
    render(<PremiumRagPipelineHero />);
    expect(screen.getByRole("heading", { name: fr.heroTitle })).toBeInTheDocument();
    expect(screen.getByText(fr.heroSubtitle)).toBeInTheDocument();
    for (const key of [
      "heroDocuments", "heroIngestion", "heroVision", "heroEmbeddings", "heroIndexing",
      "heroRetrieval", "heroReranking", "heroLlm", "heroAccess",
    ]) {
      expect(screen.getByText(fr[key]), key).toBeInTheDocument();
    }
  });

  it("montre une réponse sourcée, deux sources et l'indicateur de vérifiabilité", () => {
    render(<PremiumRagPipelineHero />);
    const answer = screen.getByTestId("rag-hero-answer");
    expect(answer).toHaveTextContent(fr.heroAnswer);
    expect(answer).toHaveTextContent(fr.heroSource1);
    expect(answer).toHaveTextContent(fr.heroSource2);
    expect(answer).toHaveTextContent(fr.heroVerifiable);
    expect(screen.getByText(fr.heroQuestion)).toBeInTheDocument();
  });

  it("place le LLM APRÈS la recherche et le reranking (jamais comme source)", () => {
    const { container } = render(<PremiumRagPipelineHero />);
    const order = Array.from(container.querySelectorAll("[data-module]")).map((n) => n.getAttribute("data-module"));
    expect(order).toEqual(["ingest", "vision", "embed", "index", "search", "rerank", "llm"]);
  });

  it("représente les formats de documents sans aucun nom de fichier réel", () => {
    const { container } = render(<PremiumRagPipelineHero />);
    const docs = Array.from(container.querySelectorAll("[data-doc]")).map((n) => n.getAttribute("data-doc"));
    expect(docs).toEqual(["PDF", "DOCX", "PPTX", "IMG", "FOLDER"]);
    expect(container.textContent).not.toMatch(/\.(pdf|docx|pptx|png|jpg)\b/i);
  });

  it("n'émet aucune requête réseau et ne laisse aucun timer", () => {
    vi.useFakeTimers();
    const fetchSpy = vi.spyOn(globalThis, "fetch");
    const xhrOpen = vi.spyOn(XMLHttpRequest.prototype, "open");
    const { unmount } = render(<PremiumRagPipelineHero />);
    expect(vi.getTimerCount()).toBe(0);
    unmount();
    expect(fetchSpy).not.toHaveBeenCalled();
    expect(xhrOpen).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("masque la scène aux lecteurs d'écran et fournit une description textuelle", () => {
    const { container } = render(<PremiumRagPipelineHero />);
    const scene = screen.getByTestId("rag-hero-scene");
    expect(scene).toHaveAttribute("aria-hidden", "true");
    expect(scene).toHaveAttribute("focusable", "false");
    expect(screen.getByText(fr.heroA11y)).toBeInTheDocument();
    expect(container.querySelector("figure")).toHaveAttribute("aria-labelledby", "rag-hero-title");
  });

  it("n'ajoute aucun élément focalisable (ordre de tabulation du formulaire intact)", () => {
    const { container } = render(<PremiumRagPipelineHero />);
    expect(container.querySelectorAll("a, button, input, select, textarea, [tabindex]")).toHaveLength(0);
  });

  it("dispose des mêmes libellés en anglais, conformes au lot", () => {
    const heroKeys = Object.keys(fr).filter((k) => k.startsWith("hero"));
    expect(heroKeys.length).toBeGreaterThanOrEqual(18);
    for (const k of heroKeys) expect(en[k], k).toBeTruthy();
    expect(en.heroTitle).toBe("From documents to verifiable answers.");
    expect(en.heroRetrieval).toBe("Retrieval");
    expect(en.heroIndexing).toBe("Indexing");
    expect(en.heroAnswer).toBe("Cited answer");
    expect(en.heroAccess).toBe("Controlled access");
    expect(fr.heroTitle).toBe("Du document à la réponse vérifiable.");
  });
});
