/**
 * Sélecteur de langue de RÉPONSE IA (auto|fr|en) — distinct de l'interface.
 * Vérifie : lecture initiale de la préférence, bascule persistée, rollback si
 * échec backend, options accessibles.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import frAnswer from "@/../messages/fr/answerLanguage.json";

const getPreferences = vi.fn();
const updateAnswerLanguage = vi.fn();
vi.mock("@/lib/api", () => ({
  getPreferences: (...a: any[]) => getPreferences(...a),
  updateAnswerLanguage: (...a: any[]) => updateAnswerLanguage(...a),
}));

import { AnswerLanguageSelector } from "@/components/AnswerLanguageSelector";

function renderSel() {
  return render(
    <NextIntlClientProvider locale="fr" messages={{ answerLanguage: frAnswer }}>
      <AnswerLanguageSelector />
    </NextIntlClientProvider>
  );
}

describe("AnswerLanguageSelector", () => {
  beforeEach(() => {
    getPreferences.mockReset();
    updateAnswerLanguage.mockReset();
    getPreferences.mockResolvedValue({ preferred_locale: "fr", preferred_answer_language: "auto" });
    updateAnswerLanguage.mockResolvedValue({ preferred_locale: "fr", preferred_answer_language: "en" });
  });

  it("lit la préférence initiale et l'affiche", async () => {
    getPreferences.mockResolvedValue({ preferred_locale: "fr", preferred_answer_language: "en" });
    renderSel();
    await waitFor(() =>
      expect(screen.getByRole("button", { name: frAnswer.aria })).toHaveTextContent(frAnswer.en)
    );
  });

  it("persiste la bascule vers en (PATCH préférences)", async () => {
    renderSel();
    await waitFor(() => expect(getPreferences).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: frAnswer.aria }));
    fireEvent.click(screen.getByRole("option", { name: new RegExp(frAnswer.en) }));
    await waitFor(() => expect(updateAnswerLanguage).toHaveBeenCalledWith("en"));
  });

  it("revient à l'état précédent si le backend échoue (rollback)", async () => {
    updateAnswerLanguage.mockRejectedValueOnce(new Error("500"));
    renderSel();
    await waitFor(() => expect(getPreferences).toHaveBeenCalled());
    const trigger = screen.getByRole("button", { name: frAnswer.aria });
    fireEvent.click(trigger);
    fireEvent.click(screen.getByRole("option", { name: new RegExp(frAnswer.en) }));
    await waitFor(() => expect(updateAnswerLanguage).toHaveBeenCalled());
    // Après rollback, l'affichage revient à "Automatique".
    await waitFor(() => expect(trigger).toHaveTextContent(frAnswer.auto));
  });

  it("expose trois options accessibles", async () => {
    renderSel();
    await waitFor(() => expect(getPreferences).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: frAnswer.aria }));
    expect(screen.getAllByRole("option")).toHaveLength(3);
  });
});
