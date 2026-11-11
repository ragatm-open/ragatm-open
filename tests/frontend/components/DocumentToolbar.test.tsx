import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { DocumentToolbar } from "@/app/doc/[documentId]/_components/DocumentToolbar";

describe("DocumentToolbar", () => {
  it("affiche le titre et le numéro de page", () => {
    render(
      <DocumentToolbar title="Mon doc" page={3} onBack={vi.fn()} onDownload={vi.fn()} openHref="/doc/x" />
    );
    expect(screen.getByText("Mon doc")).toBeInTheDocument();
    expect(screen.getByText("Page 3")).toBeInTheDocument();
  });

  // Changement de contrat assumé (lot XLSX) : « Page — » était affiché pour TOUT
  // document sans pagination, y compris un classeur — qui n'a pas de pages
  // (`page_number` est None à l'ingestion Excel : une feuille n'est pas une page).
  // Ce libellé de remplissage laissait croire à une pagination absente.
  it("n'affiche aucune ligne de page quand il n'y a pas de page", () => {
    render(
      <DocumentToolbar title="Doc" page={null} onBack={vi.fn()} onDownload={vi.fn()} openHref="/doc/x" />
    );
    expect(screen.queryByText("Page —")).not.toBeInTheDocument();
    expect(screen.queryByText(/^Page /)).not.toBeInTheDocument();
  });

  it("affiche feuille et plages pour un classeur, jamais de page", () => {
    render(
      <DocumentToolbar
        title="REFERENTIEL.xlsx"
        page={null}
        sheetName="Proposition liste données princ"
        cellRanges={["A749:D761"]}
        onBack={vi.fn()}
        onDownload={vi.fn()}
      />
    );
    expect(
      screen.getByText(/Feuille : Proposition liste données princ/)
    ).toBeInTheDocument();
    expect(screen.getByText(/Cellules : A749:D761/)).toBeInTheDocument();
    expect(screen.queryByText(/^Page /)).not.toBeInTheDocument();
  });

  it("joint plusieurs plages citées (chunk découpé par colonnes)", () => {
    render(
      <DocumentToolbar
        title="x.xlsx"
        sheetName="Données"
        cellRanges={["A749:A761", "M749:P761"]}
        onBack={vi.fn()}
        onDownload={vi.fn()}
      />
    );
    expect(screen.getByText(/Cellules : A749:A761, M749:P761/)).toBeInTheDocument();
  });

  it("n'affiche pas le lien Ouvrir sans href rendable", () => {
    render(<DocumentToolbar title="Doc" onBack={vi.fn()} onDownload={vi.fn()} />);
    expect(screen.queryByRole("link", { name: /Ouvrir/ })).not.toBeInTheDocument();
  });

  it("le téléchargement parle de l'original, jamais du PDF", () => {
    render(<DocumentToolbar title="Doc" onBack={vi.fn()} onDownload={vi.fn()} />);
    const btn = screen.getByRole("button", { name: /Télécharger/ });
    expect(btn.getAttribute("title")).toBe("Télécharger l'original");
    expect(screen.queryByTitle("Télécharger le PDF")).not.toBeInTheDocument();
  });

  it("boutons Retour/Télécharger appellent les callbacks", () => {
    const onBack = vi.fn();
    const onDownload = vi.fn();
    render(
      <DocumentToolbar title="Doc" page={1} onBack={onBack} onDownload={onDownload} openHref="/doc/x" />
    );
    fireEvent.click(screen.getByRole("button", { name: /Retour/ }));
    fireEvent.click(screen.getByRole("button", { name: /Télécharger/ }));
    expect(onBack).toHaveBeenCalled();
    expect(onDownload).toHaveBeenCalled();
  });

  it("lien Ouvrir avec href et target _blank", () => {
    render(
      <DocumentToolbar title="Doc" page={1} onBack={vi.fn()} onDownload={vi.fn()} openHref="/doc/abc?page=1" />
    );
    const link = screen.getByRole("link", { name: /Ouvrir/ }) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toBe("/doc/abc?page=1");
    expect(link.getAttribute("target")).toBe("_blank");
  });
});
