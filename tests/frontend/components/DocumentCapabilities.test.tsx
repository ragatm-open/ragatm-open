/**
 * Écran « Capacités documentaires » — superuser, LECTURE SEULE.
 * Rendu à partir d'un instantané dérivé de la réponse backend réelle
 * (fixtures/documentCapabilitiesSnapshot.json).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, within, waitFor, fireEvent } from "@testing-library/react";
import type { DocumentCapabilitiesSnapshot } from "@/lib/coreConfig";
import { ApiError } from "@/lib/apiError";
import snapshotJson from "../fixtures/documentCapabilitiesSnapshot.json";

const state = vi.hoisted(() => ({ isGlobalOwner: true, loading: false }));
const getDocumentCapabilities = vi.hoisted(() => vi.fn());

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: any) => (
    <a href={typeof href === "string" ? href : "#"} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/components/shell", () => ({
  useWorkspace: () => ({ isGlobalOwner: state.isGlobalOwner, loading: state.loading }),
  AppShell: ({ children }: any) => <>{children}</>,
}));
vi.mock("@/lib/coreConfig", async (importActual) => {
  const actual = (await importActual()) as Record<string, unknown>;
  return { ...actual, getDocumentCapabilities: (...a: unknown[]) => getDocumentCapabilities(...a) };
});

import { DocumentCapabilitiesScreen } from "@/app/admin-core/document-capabilities/_components/DocumentCapabilitiesScreen";

const SNAP = snapshotJson as unknown as DocumentCapabilitiesSnapshot;
const clone = (): DocumentCapabilitiesSnapshot => JSON.parse(JSON.stringify(SNAP));

async function renderLoaded(snap: DocumentCapabilitiesSnapshot = clone()) {
  getDocumentCapabilities.mockResolvedValue(snap);
  render(<DocumentCapabilitiesScreen />);
  await screen.findByTestId("dc-environment");
}

const MUTATION_WORDS = /^(Activer|Désactiver|Modifier|Relancer|Enregistrer|Réindexer|Importer|Supprimer|Redémarrer)\b/i;

describe("Capacités documentaires — accès", () => {
  beforeEach(() => {
    state.isGlobalOwner = true;
    state.loading = false;
    getDocumentCapabilities.mockReset();
  });

  it("écran verrouillé pour un non-superuser, sans aucun appel réseau", () => {
    state.isGlobalOwner = false;
    render(<DocumentCapabilitiesScreen />);
    expect(screen.getByText("Accès réservé")).toBeInTheDocument();
    expect(getDocumentCapabilities).not.toHaveBeenCalled();
  });

  it("403 backend → message d'accès refusé", async () => {
    getDocumentCapabilities.mockRejectedValue(new ApiError(403, "Forbidden", { detail: "forbidden" }, "API 403"));
    render(<DocumentCapabilitiesScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent(/Accès refusé/);
  });

  it("erreur générique → bandeau d'erreur", async () => {
    getDocumentCapabilities.mockRejectedValue(new ApiError(500, "Server Error", null, "API 500"));
    render(<DocumentCapabilitiesScreen />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Les capacités documentaires n'ont pas pu être chargées.");
  });

  it("état de chargement (squelette)", () => {
    getDocumentCapabilities.mockReturnValue(new Promise(() => {}));
    render(<DocumentCapabilitiesScreen />);
    expect(screen.getByRole("status")).toHaveTextContent("Chargement des capacités…");
  });
});

describe("Capacités documentaires — rendu", () => {
  beforeEach(() => {
    state.isGlobalOwner = true;
    state.loading = false;
    getDocumentCapabilities.mockReset();
  });

  it("en-tête : environnement et SHA lus dans la réponse", async () => {
    await renderLoaded();
    expect(getDocumentCapabilities).toHaveBeenCalledWith({ ragspaceId: null, refresh: false });
    expect(screen.getByTestId("dc-environment")).toHaveTextContent("STAGING");
    expect(screen.getByTestId("dc-sha")).toHaveTextContent("a1b2c3d4");
    expect(screen.getByRole("heading", { level: 1, name: "Capacités documentaires" })).toBeInTheDocument();
    expect(screen.getByText(`${SNAP.warnings.length} avertissements`)).toBeInTheDocument();
  });

  it("l'environnement n'est jamais codé en dur", async () => {
    const snap = clone();
    snap.environment = "prod";
    snap.deployment_sha = "";
    await renderLoaded(snap);
    expect(screen.getByTestId("dc-environment")).toHaveTextContent("PROD");
    expect(screen.getByTestId("dc-sha")).toHaveTextContent("non renseignée");
  });

  it("légende : chaque état a un libellé texte", async () => {
    await renderLoaded();
    const legend = screen.getByText("Légende des statuts").closest("details")!;
    for (const label of [
      "Actif (global)",
      "Actif (espaces autorisés)",
      "Actif sous condition",
      "Désactivé",
      "Non supporté",
      "Indisponible",
      "Incohérent",
      "Non probant",
      "Expérimental",
      "Non qualifié",
      "Partiel",
      "Qualifié synthétique",
      "Qualifié documents réels",
      "Non mesuré",
    ]) {
      expect(within(legend).getAllByText(label).length, label).toBeGreaterThan(0);
    }
  });

  it("matrice : table sémantique avec en-têtes et une ligne par format", async () => {
    await renderLoaded();
    const table = within(screen.getByTestId("dc-matrix-table")).getByRole("table");
    for (const col of ["Format", "Texte", "OCR", "Images intégrées", "Vision", "Tableaux", "Graphiques", "Audio", "AI Gateway"]) {
      expect(within(table).getByRole("columnheader", { name: col })).toHaveAttribute("scope", "col");
    }
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(SNAP.matrix.length);
    const pdfRow = rows.find((r) => r.getAttribute("data-format-id") === "pdf")!;
    expect(within(pdfRow).getByRole("rowheader")).toHaveTextContent("PDF");
    expect(within(pdfRow).getAllByText("Actif (global)").length).toBeGreaterThan(0);
    expect(within(pdfRow).getByText("1/5 actif(s)")).toBeInTheDocument(); // tableaux : 1 actif sur 5
    expect(pdfRow).toHaveAttribute("tabindex", "0");
  });

  it("sélection d'une ligne (clic et clavier) → panneau de détail", async () => {
    await renderLoaded();
    const table = within(screen.getByTestId("dc-matrix-table")).getByRole("table");
    // Par défaut : premier format
    expect(screen.getByRole("heading", { name: "Détail du format · PDF" })).toBeInTheDocument();
    const audioRow = within(table).getByRole("row", { name: "Afficher le détail du format Audio" });
    fireEvent.keyDown(audioRow, { key: "Enter" });
    expect(screen.getByRole("heading", { name: "Détail du format · Audio" })).toBeInTheDocument();
    const detail = screen.getByTestId("dc-detail");
    // Taille décimale de l'audio
    expect(within(detail).getByText(/600 Mo/)).toBeInTheDocument();
    // audio.asr : INCONSISTENT + 0 espace → inopérant
    const asr = detail.querySelector('[data-capability-id="audio.asr"]') as HTMLElement;
    expect(within(asr).getByText("Inopérant : aucun espace autorisé")).toBeInTheDocument();
    expect(within(asr).getAllByText("Incohérent").length).toBeGreaterThan(0);
    // dépendance Gateway affichée (service + modèles + état)
    expect(within(asr).getByText("asr")).toBeInTheDocument();
    expect(within(asr).getByText(/whisper-turbo/)).toBeInTheDocument();
    expect(within(asr).getAllByText("Indisponible").length).toBeGreaterThan(0);
    // capacités communes incluses
    expect(detail.querySelector('[data-capability-id="common.embeddings"]')).not.toBeNull();

    const docxRow = within(table).getByRole("row", { name: "Afficher le détail du format Word (DOCX)" });
    fireEvent.click(docxRow);
    expect(screen.getByRole("heading", { name: "Détail du format · Word (DOCX)" })).toBeInTheDocument();
    // capacités visuelles transverses incluses pour docx
    expect(screen.getByTestId("dc-detail").querySelector('[data-capability-id="visual.structx.table"]')).not.toBeNull();
  });

  it("détail : statut, local vs Gateway, qualification, « Non probant », détails techniques", async () => {
    await renderLoaded();
    const detail = screen.getByTestId("dc-detail");
    const text = detail.querySelector('[data-capability-id="pdf.text"]') as HTMLElement;
    expect(within(text).getByText("Local (sans AI Gateway)")).toBeInTheDocument();
    expect(within(text).getAllByText("Qualifié documents réels").length).toBeGreaterThan(0);
    expect(within(text).getByText("Détails techniques")).toBeInTheDocument();
    expect(within(text).getByText("DOCUMENT_PDF_NATIVE_FALLBACK_ENABLED")).toBeInTheDocument();
    const vision = detail.querySelector('[data-capability-id="pdf.inline_image.vision"]') as HTMLElement;
    expect(within(vision).getAllByText("Non probant").length).toBeGreaterThan(0);
    expect(within(vision).getByText("vision")).toBeInTheDocument();
    expect(within(vision).getByText(/\/v1\/vision\/analyze/)).toBeInTheDocument();
  });

  it("filtres : famille « Refusés », masquer non supportés, recherchable", async () => {
    await renderLoaded();
    const all = screen.getByTestId("dc-all-formats");
    expect(within(all).getAllByText("Refusé").length).toBeGreaterThan(0);
    expect(within(all).getByText(/\.exe/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("checkbox", { name: "Masquer les non supportés" }));
    expect(within(screen.getByTestId("dc-all-formats")).queryByText(/\.exe/)).toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: "Masquer les non supportés" }));

    fireEvent.click(screen.getByRole("button", { name: "Audio" }));
    expect(screen.getByRole("button", { name: "Audio" })).toHaveAttribute("aria-pressed", "true");
    const table = within(screen.getByTestId("dc-matrix-table")).getByRole("table");
    expect(within(table).getAllByRole("row").slice(1).map((r) => r.getAttribute("data-format-id"))).toEqual(["audio"]);

    fireEvent.click(screen.getByRole("button", { name: "Tous" }));
    fireEvent.click(screen.getByRole("checkbox", { name: "Seulement ce qui produit du contenu recherchable" }));
    const ids = within(within(screen.getByTestId("dc-matrix-table")).getByRole("table"))
      .getAllByRole("row")
      .slice(1)
      .map((r) => r.getAttribute("data-format-id"));
    expect(ids).toContain("pdf");
    expect(ids).not.toContain("audio");
  });

  it("tous les formats : groupes et extensions", async () => {
    await renderLoaded();
    expect(screen.getByText(`${SNAP.summary.formats} groupes de formats · ${SNAP.summary.extensions} extensions`)).toBeInTheDocument();
    const all = screen.getByTestId("dc-all-formats");
    expect(all.querySelectorAll("li[data-format-id]")).toHaveLength(SNAP.formats.length);
  });

  it("images intégrées : lignes calculées depuis les capacités", async () => {
    await renderLoaded();
    const table = screen.getByTestId("dc-inline-images");
    for (const id of ["pdf", "docx", "xlsx", "msg", "image"]) {
      expect(table.querySelector(`tr[data-format-id="${id}"]`), id).not.toBeNull();
    }
    expect(within(table).getByRole("columnheader", { name: "Description Vision" })).toBeInTheDocument();
  });

  it("services AI Gateway : statuts, expérimental, qualification, describe/semantic informatifs", async () => {
    await renderLoaded();
    const list = screen.getByTestId("dc-gateway-services");
    const vision = list.querySelector('[data-service="vision"]') as HTMLElement;
    expect(within(vision).getByText("Disponible")).toBeInTheDocument();
    expect(within(vision).getByText("Description (describe) disponible")).toBeInTheDocument();
    expect(within(vision).getByText(/Analyse sémantique non activée \(information/)).toBeInTheDocument();
    const sx = list.querySelector('[data-service="structured_extract"]') as HTMLElement;
    expect(within(sx).getByText("Expérimental")).toBeInTheDocument();
    expect(within(sx).getByText("Non qualifié pour la production")).toBeInTheDocument();
    expect(within(sx).getByText(/chart : NOT_QUALIFIED/)).toBeInTheDocument();
    expect(within(list.querySelector('[data-service="asr"]') as HTMLElement).getByText("Indisponible")).toBeInTheDocument();
    expect(within(list.querySelector('[data-service="embeddings"]') as HTMLElement).getByText("Inconnu")).toBeInTheDocument();
    expect(within(list.querySelector('[data-service="rerank"]') as HTMLElement).getByText("Dégradé")).toBeInTheDocument();
    expect(screen.getByText(/Un service disponible ne signifie pas qu'une tâche est qualifiée/)).toBeInTheDocument();
  });

  it("embeddings : compteurs, fournisseurs, défaut et repli", async () => {
    await renderLoaded();
    const emb = screen.getByTestId("dc-embeddings");
    expect(within(emb).getByText("nomic-embed-text")).toBeInTheDocument();
    expect(within(emb).getByText("fr-en-retrieval")).toBeInTheDocument();
    expect(within(emb).getByText("ollama / nomic-embed-text")).toBeInTheDocument();
    expect(within(emb).getByText("activé")).toBeInTheDocument();
    expect(screen.getByText("18 espaces au total")).toBeInTheDocument();
  });

  it("cohérence d'exécution : rôles, divergences, différences ignorées", async () => {
    await renderLoaded();
    const rt = screen.getByTestId("dc-runtime");
    expect(within(rt).getByText("Récente")).toBeInTheDocument();
    expect(within(rt).getByText("Périmée")).toBeInTheDocument();
    expect(within(rt).getByText("DOCUMENT_PDF_OCR_ENABLED diffère sur : backend, worker")).toBeInTheDocument();
    expect(within(rt).getByText("3 différences ignorées")).toBeInTheDocument();
    expect(screen.getAllByText("Partielle").length).toBeGreaterThan(0);
  });

  it("avertissements traduits en français (code inconnu inclus)", async () => {
    await renderLoaded();
    const w = screen.getByTestId("dc-warnings");
    for (const sentence of [
      "Capacité activée mais aucun espace n'est autorisé : elle est inopérante.",
      "La liste d'espaces autorisés contient des entrées inexistantes ou invalides.",
      "Un réglage diffère entre des services qui le consomment.",
      "Un service n'a pas publié d'empreinte de configuration récente.",
      "Un service tourne sur une version déployée différente.",
      "Un service AI Gateway est indisponible.",
      "Un service AI Gateway est dégradé.",
    ]) {
      expect(within(w).getByText(sentence)).toBeInTheDocument();
    }
    expect(within(w).getByText("État non vérifiable.")).toBeInTheDocument();
  });

  it("Gateway inconnue partout → « Inconnu », jamais « Disponible »", async () => {
    const snap = clone();
    snap.gateway_services = snap.gateway_services.map((s) => ({ service: s.service, status: "UNKNOWN", reason: "not_configured" }));
    await renderLoaded(snap);
    const list = screen.getByTestId("dc-gateway-services");
    expect(within(list).getAllByText("Inconnu")).toHaveLength(snap.gateway_services.length);
    expect(within(list).queryByText("Disponible")).toBeNull();
  });

  it("« Actualiser » appelle le backend avec refresh=1", async () => {
    await renderLoaded();
    getDocumentCapabilities.mockClear();
    fireEvent.click(screen.getByRole("button", { name: /Actualiser l'instantané/ }));
    await waitFor(() => expect(getDocumentCapabilities).toHaveBeenCalledWith({ ragspaceId: null, refresh: true }));
  });

  it("aucun bouton de mutation (activer, modifier, relancer, enregistrer…)", async () => {
    await renderLoaded();
    const buttons = screen.getAllByRole("button");
    for (const b of buttons) {
      expect(b.textContent ?? "", b.outerHTML.slice(0, 80)).not.toMatch(MUTATION_WORDS);
      expect(b.getAttribute("aria-label") ?? "").not.toMatch(MUTATION_WORDS);
    }
    expect(screen.queryByText(/^(Activer|Désactiver|Modifier|Relancer|Enregistrer)$/)).toBeNull();
  });
});

describe("Capacités documentaires — vue par espace", () => {
  beforeEach(() => {
    state.isGlobalOwner = true;
    state.loading = false;
    getDocumentCapabilities.mockReset();
  });

  it("applique l'UUID, affiche le nom, « S'applique à cet espace » et le fournisseur d'embeddings", async () => {
    const global = clone();
    const spaced = clone();
    spaced.ragspace_view = { id: "11111111-2222-3333-4444-555555555555", name: "Espace Alpha" };
    spaced.embeddings.selected_space = { provider: "ai_gateway", model: "legacy-768", via_ai_gateway: true };
    getDocumentCapabilities.mockResolvedValueOnce(global).mockResolvedValueOnce(spaced);
    render(<DocumentCapabilitiesScreen />);
    await screen.findByTestId("dc-environment");
    fireEvent.change(screen.getByLabelText("Identifiant de l'espace (UUID)"), {
      target: { value: "11111111-2222-3333-4444-555555555555" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Afficher pour cet espace" }));
    await screen.findByTestId("dc-space-active");
    expect(getDocumentCapabilities).toHaveBeenLastCalledWith({
      ragspaceId: "11111111-2222-3333-4444-555555555555",
      refresh: false,
    });
    expect(screen.getByTestId("dc-space-active")).toHaveTextContent("Vue de l'espace « Espace Alpha »");
    const detail = screen.getByTestId("dc-detail");
    const pdfText = detail.querySelector('[data-capability-id="pdf.text"]') as HTMLElement;
    expect(within(pdfText).getByTestId("dc-space-applicable")).toHaveTextContent("S'applique à cet espace : oui");
    const pdfOcr = detail.querySelector('[data-capability-id="pdf.ocr"]') as HTMLElement;
    expect(within(pdfOcr).getByTestId("dc-space-applicable")).toHaveTextContent("S'applique à cet espace : non");
    expect(screen.getByTestId("dc-selected-space-embeddings")).toHaveTextContent("ai_gateway / legacy-768 · via AI Gateway");
  });

  it("404 / 400 → message, l'instantané global reste affiché", async () => {
    getDocumentCapabilities
      .mockResolvedValueOnce(clone())
      .mockRejectedValueOnce(new ApiError(404, "Not Found", { detail: "ragspace_not_found" }, "API 404"))
      .mockRejectedValueOnce(new ApiError(400, "Bad Request", { detail: "ragspace_id_invalid" }, "API 400"));
    render(<DocumentCapabilitiesScreen />);
    await screen.findByTestId("dc-environment");
    const input = screen.getByLabelText("Identifiant de l'espace (UUID)");
    fireEvent.change(input, { target: { value: "11111111-2222-3333-4444-555555555555" } });
    fireEvent.click(screen.getByRole("button", { name: "Afficher pour cet espace" }));
    expect(await screen.findByText("Espace introuvable.")).toBeInTheDocument();
    expect(screen.getByTestId("dc-environment")).toHaveTextContent("STAGING");
    fireEvent.change(input, { target: { value: "pas-un-uuid" } });
    fireEvent.click(screen.getByRole("button", { name: "Afficher pour cet espace" }));
    expect(await screen.findByText("Identifiant d'espace invalide (UUID attendu).")).toBeInTheDocument();
  });
});
