import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { DocumentSnippet } from "@/app/doc/[documentId]/_components/DocumentSnippet";

describe("DocumentSnippet", () => {
  let writeText: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
  });
  afterEach(() => vi.useRealTimers());

  it("affiche le snippet", () => {
    render(<DocumentSnippet snippet="texte cité" />);
    expect(screen.getByText("texte cité")).toBeInTheDocument();
  });

  it("copie le snippet dans le presse-papier et affiche 'Copié'", async () => {
    render(<DocumentSnippet snippet="à copier" />);
    fireEvent.click(screen.getByRole("button", { name: "Copier" }));
    expect(writeText).toHaveBeenCalledWith("à copier");
    await waitFor(() => expect(screen.getByRole("button", { name: "Copié ✅" })).toBeInTheDocument());
  });

  it("revient à 'Copier' après le délai", async () => {
    vi.useFakeTimers();
    render(<DocumentSnippet snippet="x" />);
    fireEvent.click(screen.getByRole("button", { name: "Copier" }));
    // laisser la promesse clipboard se résoudre
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByRole("button", { name: "Copié ✅" })).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(1300);
    });
    expect(screen.getByRole("button", { name: "Copier" })).toBeInTheDocument();
  });
});
