/**
 * Sélecteur de langue — bascule fr ⇄ en, cookie, sync profil, a11y.
 *
 * Vérifie le contrat de la mission (§16.2) : écriture du cookie, appel de
 * synchronisation profil, rafraîchissement des Server Components, et attributs
 * d'accessibilité (menu clavier + ARIA). L'échec backend ne doit pas bloquer.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";

import frAccessibility from "@/../messages/fr/accessibility.json";

const refresh = vi.fn();
vi.mock("next/navigation", () => ({
  useRouter: () => ({ refresh }),
}));

const apiPatch = vi.fn().mockResolvedValue({ preferred_locale: "en" });
vi.mock("@/lib/api", () => ({
  apiPatch: (...a: any[]) => apiPatch(...a),
}));

import { LanguageSelector } from "@/components/LanguageSelector";

function renderSelector(locale: "fr" | "en" = "fr") {
  // `useLocale` est shimé globalement (cf. setup.ts) et lit `__TEST_LOCALE__`.
  (globalThis as any).__TEST_LOCALE__ = locale;
  return render(
    <NextIntlClientProvider locale={locale} messages={{ accessibility: frAccessibility }}>
      <LanguageSelector />
    </NextIntlClientProvider>
  );
}

describe("LanguageSelector", () => {
  beforeEach(() => {
    // Réinitialise le cookie et la locale de test entre les cas.
    document.cookie = "ragatm_locale=; path=/; max-age=0";
    (globalThis as any).__TEST_LOCALE__ = "fr";
    refresh.mockClear();
    apiPatch.mockClear();
  });

  it("affiche la langue active et les attributs ARIA du déclencheur", () => {
    renderSelector("fr");
    const trigger = screen.getByRole("button", { name: frAccessibility.showLanguages });
    expect(trigger).toHaveAttribute("aria-haspopup", "listbox");
    expect(trigger).toHaveAttribute("aria-expanded", "false");
    expect(trigger).toHaveTextContent("FR");
  });

  it("ouvre le menu et expose des options accessibles", () => {
    renderSelector("fr");
    fireEvent.click(screen.getByRole("button", { name: frAccessibility.showLanguages }));
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(2);
    // fr est sélectionné, en ne l'est pas.
    const fr = options.find((o) => o.textContent?.includes("Français"))!;
    const en = options.find((o) => o.textContent?.includes("English"))!;
    expect(fr).toHaveAttribute("aria-selected", "true");
    expect(en).toHaveAttribute("aria-selected", "false");
  });

  it("bascule fr → en : écrit le cookie, sync le profil, rafraîchit", async () => {
    renderSelector("fr");
    fireEvent.click(screen.getByRole("button", { name: frAccessibility.showLanguages }));
    fireEvent.click(screen.getByRole("option", { name: /English/ }));

    expect(document.cookie).toContain("ragatm_locale=en");
    expect(apiPatch).toHaveBeenCalledWith("/api/v1/me/preferences/", { preferred_locale: "en" });
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("bascule en → fr depuis l'état anglais", async () => {
    renderSelector("en");
    fireEvent.click(screen.getByRole("button", { name: frAccessibility.showLanguages }));
    fireEvent.click(screen.getByRole("option", { name: /Français/ }));

    expect(document.cookie).toContain("ragatm_locale=fr");
    expect(apiPatch).toHaveBeenCalledWith("/api/v1/me/preferences/", { preferred_locale: "fr" });
  });

  it("ne bloque pas si la synchronisation profil échoue (fail-open)", async () => {
    apiPatch.mockRejectedValueOnce(new Error("401"));
    renderSelector("fr");
    fireEvent.click(screen.getByRole("button", { name: frAccessibility.showLanguages }));
    fireEvent.click(screen.getByRole("option", { name: /English/ }));

    // Le cookie est posé et le rafraîchissement a lieu malgré l'échec backend.
    expect(document.cookie).toContain("ragatm_locale=en");
    await waitFor(() => expect(refresh).toHaveBeenCalled());
  });

  it("ne fait rien si on resélectionne la langue active", () => {
    renderSelector("fr");
    fireEvent.click(screen.getByRole("button", { name: frAccessibility.showLanguages }));
    fireEvent.click(screen.getByRole("option", { name: /Français/ }));
    expect(apiPatch).not.toHaveBeenCalled();
    expect(refresh).not.toHaveBeenCalled();
  });
});
