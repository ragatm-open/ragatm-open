import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { FolderImportModal } from "@/app/documents/_components/FolderImportModal";
import {
  buildFolderPlan,
  defaultSelectedFormats,
  extensionsOfFormats,
  summarizeFolderFormats,
} from "@/app/documents/_lib/folderImport";

function fakeFile(relPath: string, size: number): File {
  const name = relPath.split("/").pop() || relPath;
  const f = new File([new Uint8Array(Math.max(0, size))], name, { type: "" });
  Object.defineProperty(f, "webkitRelativePath", { value: relPath, configurable: true });
  return f;
}

const MAX = 25 * 1024 * 1024;

const MIX = [
  fakeFile("HR/root.pdf", 1000),
  fakeFile("HR/policies/leave.docx", 2000),
  fakeFile("HR/images/diagram.png", 3000),
  fakeFile("HR/ignored/binary.exe", 4000), // non supporté
];

/** Rend la modale avec la sélection donnée (par défaut : tout coché). */
function renderModal(
  files: File[] = MIX,
  selectedKeys?: string[],
  handlers: { onCancel?: () => void; onConfirm?: () => void; onToggleFormat?: (k: string) => void } = {}
) {
  const formats = summarizeFolderFormats(files);
  const selected = selectedKeys ? new Set(selectedKeys) : defaultSelectedFormats(formats);
  const plan = buildFolderPlan(files, {
    maxSizeBytes: MAX,
    selectedExtensions: extensionsOfFormats(selected),
  });
  const utils = render(
    <FolderImportModal
      plan={plan}
      formats={formats}
      selected={selected}
      onToggleFormat={handlers.onToggleFormat ?? vi.fn()}
      maxSizeMb={25}
      onCancel={handlers.onCancel ?? vi.fn()}
      onConfirm={handlers.onConfirm ?? vi.fn()}
    />
  );
  return { ...utils, plan, formats, selected };
}

describe("FolderImportModal — sélection des formats", () => {
  it("n'affiche que les formats présents dans le dossier, avec leur volumétrie", () => {
    renderModal();
    expect(screen.getByRole("checkbox", { name: /PDF/i })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Word/i })).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: /Images/i })).toBeInTheDocument();
    // Aucun fichier Excel dans ce dossier => aucune case Excel.
    expect(screen.queryByRole("checkbox", { name: /Excel/i })).not.toBeInTheDocument();
    // Le compteur par format est du TEXTE (jamais une information de couleur seule).
    expect(screen.getByRole("checkbox", { name: /PDF.*1 fichier/i })).toBeInTheDocument();
  });

  it("tous les formats sont COCHÉS par défaut", () => {
    renderModal();
    for (const cb of screen.getAllByRole("checkbox")) expect(cb).toBeChecked();
  });

  it("décocher un format remonte la bascule au parent", () => {
    const onToggleFormat = vi.fn();
    renderModal(MIX, undefined, { onToggleFormat });
    fireEvent.click(screen.getByRole("checkbox", { name: /PDF/i }));
    expect(onToggleFormat).toHaveBeenCalledWith("pdf");
  });

  it("décocher PDF recalcule le récapitulatif (le PDF n'est plus à importer)", () => {
    // Tout coché : les 3 fichiers supportés sont retenus.
    const all = renderModal();
    expect(all.plan.compatible).toHaveLength(3);
    expect(screen.getByText("root.pdf")).toBeInTheDocument();
    all.unmount();

    // PDF décoché : il disparaît de la liste des fichiers à importer.
    const sansPdf = renderModal(MIX, ["word", "images"]);
    expect(sansPdf.plan.compatible).toHaveLength(2);
    expect(screen.queryByText("root.pdf")).not.toBeInTheDocument();
    expect(screen.getByText("policies/leave.docx")).toBeInTheDocument();
    // Le motif « format décoché » est exposé (chip de compteur + détail par fichier).
    expect(screen.getAllByText(/format décoché/).length).toBeGreaterThan(0);
  });

  it("recocher PDF restaure le total", () => {
    const { plan } = renderModal(MIX, ["pdf", "word", "images"]);
    expect(plan.compatible).toHaveLength(3);
    expect(screen.getByText("root.pdf")).toBeInTheDocument();
  });

  it("sélection vide : import désactivé + message explicite", () => {
    const onConfirm = vi.fn();
    renderModal(MIX, [], { onConfirm });
    expect(screen.getByText(/Sélectionnez au moins un format/i)).toBeInTheDocument();
    const btn = screen.getByRole("button", { name: /Importer et indexer/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("énonce le caractère ADDITIF de l'import, sans vocabulaire destructif", () => {
    const { container } = renderModal();
    expect(screen.getByText(/déjà présents dans cet espace seront conservés/i)).toBeInTheDocument();
    const text = container.textContent || "";
    expect(text).not.toMatch(/synchronis|remplacer|réconcil|miroir/i);
  });
});

describe("FolderImportModal — récapitulatif et validation", () => {
  it("affiche les bons compteurs (détectés / à ajouter / ignorés)", () => {
    renderModal();
    expect(screen.getByText("Dossier : HR")).toBeInTheDocument();
    expect(screen.getByText("4")).toBeInTheDocument(); // détectés
    expect(screen.getByText("3")).toBeInTheDocument(); // à ajouter
    expect(screen.getByText("policies/leave.docx")).toBeInTheDocument();
  });

  it("distingue « format non supporté » de « format décoché »", () => {
    renderModal(MIX, ["images"]);
    fireEvent.click(screen.getByText(/fichiers? ignorés?/));
    expect(screen.getByText(/binary\.exe — format non supporté/)).toBeInTheDocument();
    expect(screen.getByText(/root\.pdf — format décoché/)).toBeInTheDocument();
  });

  it("Annuler n'envoie rien (onConfirm non appelé)", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    renderModal(MIX, undefined, { onCancel, onConfirm });
    fireEvent.click(screen.getByRole("button", { name: /Annuler/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("Importer et indexer déclenche onConfirm", () => {
    const onConfirm = vi.fn();
    renderModal(MIX, undefined, { onConfirm });
    fireEvent.click(screen.getByRole("button", { name: /Importer et indexer/i }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("un double clic ne déclenche QU'UN seul import", () => {
    const onConfirm = vi.fn();
    renderModal(MIX, undefined, { onConfirm });
    const btn = screen.getByRole("button", { name: /Importer et indexer/i });
    fireEvent.click(btn);
    fireEvent.click(btn);
    fireEvent.click(btn);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(btn).toBeDisabled();
  });

  it("désactive l'import quand aucun fichier compatible", () => {
    const onConfirm = vi.fn();
    renderModal([fakeFile("HR/x.exe", 10)], undefined, { onConfirm });
    const btn = screen.getByRole("button", { name: /Importer et indexer/i });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("chaque case est associée à un label cliquable (accessibilité)", () => {
    const onToggleFormat = vi.fn();
    renderModal(MIX, undefined, { onToggleFormat });
    // Le clic sur le LIBELLÉ bascule la case : label ↔ input correctement liés.
    fireEvent.click(screen.getByText("Images"));
    expect(onToggleFormat).toHaveBeenCalledWith("images");
  });

  it("Échap ferme la modale sans importer", () => {
    const onCancel = vi.fn();
    const onConfirm = vi.fn();
    renderModal(MIX, undefined, { onCancel, onConfirm });
    fireEvent.keyDown(window, { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(1);
    expect(onConfirm).not.toHaveBeenCalled();
  });
});
