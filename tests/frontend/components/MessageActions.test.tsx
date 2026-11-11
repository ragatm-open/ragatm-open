import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// Mock du client API avant import du composant.
vi.mock("@/lib/api", () => ({ apiFetch: vi.fn() }));
import { apiFetch } from "@/lib/api";
import { ApiError } from "@/lib/apiError";
import { MessageActions } from "@/app/chat/_components/MessageActions";

const mockedFetch = apiFetch as unknown as ReturnType<typeof vi.fn>;

describe("MessageActions", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    mockedFetch.mockReset();
    mockedFetch.mockResolvedValue(undefined);
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
  });
  afterEach(() => vi.useRealTimers());

  it("copie la réponse et affiche 'Copié'", async () => {
    render(<MessageActions text="ma réponse" logId="log-1" />);
    fireEvent.click(screen.getByRole("button", { name: "Copier" }));
    expect(writeText).toHaveBeenCalledWith("ma réponse");
    await waitFor(() => expect(screen.getByRole("button", { name: "Copié" })).toBeInTheDocument());
  });

  it("pouce haut envoie un feedback positif sur /chat/feedback/", async () => {
    render(<MessageActions text="r" logId="log-42" />);
    fireEvent.click(screen.getByRole("button", { name: "Réponse utile" }));

    await waitFor(() => expect(screen.getByText("Merci !")).toBeInTheDocument());
    expect(mockedFetch).toHaveBeenCalledTimes(1);
    const [url, init] = mockedFetch.mock.calls[0];
    expect(url).toBe("/api/v1/chat/feedback/");
    const body = JSON.parse((init as any).body);
    expect(body).toMatchObject({ log_id: "log-42", score: 1 });
    // Le pouce haut reste simple : aucune catégorie, aucun formulaire.
    expect(body.kind).toBeUndefined();
  });

  it("pouce bas ouvre le formulaire de motif puis envoie la catégorie", async () => {
    render(<MessageActions text="r" logId="log-7" />);
    fireEvent.click(screen.getByRole("button", { name: "Réponse à améliorer" }));

    const select = screen.getByLabelText("Motif");
    fireEvent.change(select, { target: { value: "wrong_answer" } });
    fireEvent.change(screen.getByLabelText("Détails"), { target: { value: "faux" } });
    fireEvent.click(screen.getByRole("button", { name: "Envoyer" }));

    await waitFor(() => expect(screen.getByText("Merci pour votre retour.")).toBeInTheDocument());
    const [url, init] = mockedFetch.mock.calls[0];
    expect(url).toBe("/api/v1/chat/feedback/");
    const body = JSON.parse((init as any).body);
    expect(body).toMatchObject({ log_id: "log-7", score: -1, kind: "wrong_answer", comment: "faux" });
  });

  it("propose document et terme attendus pour 'information dans mes documents'", async () => {
    render(<MessageActions text="r" logId="log-8" />);
    fireEvent.click(screen.getByRole("button", { name: "Réponse à améliorer" }));

    // La catégorie à plus forte valeur diagnostique est proposée par défaut.
    expect((screen.getByLabelText("Motif") as HTMLSelectElement).value).toBe("info_in_docs");

    fireEvent.change(screen.getByLabelText("Quel document ?"), {
      target: { value: "note-interne-2023.pdf" },
    });
    fireEvent.change(screen.getByLabelText("Quel terme ou passage ?"), {
      target: { value: "Nordexia Conseil" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Envoyer" }));

    await waitFor(() => expect(mockedFetch).toHaveBeenCalled());
    const body = JSON.parse((mockedFetch.mock.calls[0][1] as any).body);
    expect(body).toMatchObject({
      score: -1,
      kind: "info_in_docs",
      expected_document: "note-interne-2023.pdf",
      expected_term: "Nordexia Conseil",
    });
    // Commentaire FACULTATIF : absent du payload s'il est vide.
    expect(body.comment).toBeUndefined();
  });

  it("masque document/terme pour les autres catégories", () => {
    render(<MessageActions text="r" logId="log-9" />);
    fireEvent.click(screen.getByRole("button", { name: "Réponse à améliorer" }));
    fireEvent.change(screen.getByLabelText("Motif"), { target: { value: "wrong_sources" } });

    expect(screen.queryByLabelText("Quel document ?")).toBeNull();
    expect(screen.queryByLabelText("Quel terme ou passage ?")).toBeNull();
  });

  it("n'appelle JAMAIS de second endpoint quand le premier échoue", async () => {
    mockedFetch.mockRejectedValue(new ApiError(500, "Server Error", null, "boom"));
    render(<MessageActions text="r" logId="log-10" />);
    fireEvent.click(screen.getByRole("button", { name: "Réponse utile" }));

    await waitFor(() => expect(screen.getByText("Échec de l'envoi.")).toBeInTheDocument());
    // Le repli historique visait `/api/v1/audit/feedback/`, une route qui n'a
    // jamais existé : ce test échoue si elle réapparaît.
    expect(mockedFetch).toHaveBeenCalledTimes(1);
    const urls = mockedFetch.mock.calls.map((c) => c[0]);
    expect(urls).toEqual(["/api/v1/chat/feedback/"]);
    expect(urls).not.toContain("/api/v1/audit/feedback/");
  });

  it("affiche un message lisible quand un retour existe déjà (409)", async () => {
    mockedFetch.mockRejectedValue(new ApiError(409, "Conflict", null, "exists"));
    render(<MessageActions text="r" logId="log-11" />);
    fireEvent.click(screen.getByRole("button", { name: "Réponse utile" }));

    await waitFor(() =>
      expect(screen.getByText("Vous avez déjà envoyé un retour sur cette réponse.")).toBeInTheDocument(),
    );
    expect(screen.queryByText("Échec de l'envoi.")).toBeNull();
  });

  it("désactive les pouces sans log_id", () => {
    render(<MessageActions text="r" logId={null} />);
    expect(screen.getByRole("button", { name: "Réponse utile" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Réponse à améliorer" })).toBeDisabled();
  });
});
