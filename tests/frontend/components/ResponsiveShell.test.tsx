/**
 * Coquille responsive : tiroir de navigation mobile.
 *
 * jsdom ne calcule aucune mise en page : ces tests vérifient l'ÉTAT et
 * l'accessibilité (aria, inert, focus, Échap). Les dimensions réelles sont
 * qualifiées dans un vrai navigateur (cf. docs/04-frontend/frontend.md §7).
 */
import React from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ pathname: "/home" }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.pathname,
  useRouter: () => ({ refresh: vi.fn(), replace: vi.fn(), push: vi.fn() }),
}));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: any) => (
    <a href={typeof href === "string" ? href : "#"} {...rest}>
      {children}
    </a>
  ),
}));
vi.mock("@/components/shell/SpaceSwitcher", () => ({ SpaceSwitcher: () => null }));
vi.mock("@/components/shell/WorkspaceSwitcher", () => ({ WorkspaceSwitcher: () => null }));
vi.mock("@/components/shell/WorkspaceContext", () => ({
  WorkspaceProvider: ({ children }: any) => <>{children}</>,
  useWorkspace: () => ({
    me: { username: "root", email: "root@e.com" },
    role: "admin",
    isAdmin: true,
    isGlobalOwner: false,
    isSuperuser: false,
    logout: vi.fn(),
  }),
  ROLE_INFO: { admin: { labelKey: "administrator", descKey: "administratorDesc", color: "#000" } },
}));

import { AppShell, Topbar } from "@/components/shell/AppShell";

/** Simule la largeur : `mobile` fait correspondre la media query ≤ 767 px. */
function stubViewport(mobile: boolean) {
  window.matchMedia = vi.fn((query: string) => ({
    matches: mobile && /max-width:\s*(767|1199)px/.test(query),
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  })) as any;
}

function mount() {
  return render(
    <AppShell>
      <Topbar title="Accueil" />
      <p>contenu</p>
    </AppShell>
  );
}

const toggle = () => document.querySelector('[aria-controls="app-navigation"]') as HTMLButtonElement;
const drawer = () => document.getElementById("app-navigation") as HTMLElement;
const backdrop = () => document.querySelector('div[aria-hidden="true"][data-open]') as HTMLElement;

describe("Coquille — tiroir de navigation", () => {
  beforeEach(() => {
    nav.pathname = "/home";
  });
  afterEach(() => {
    delete (window as any).matchMedia;
    document.documentElement.style.overflow = "";
  });

  it("expose un bouton menu relié au tiroir (aria-controls, aria-expanded, nom)", () => {
    stubViewport(true);
    mount();
    expect(toggle()).toHaveAttribute("aria-controls", "app-navigation");
    expect(toggle()).toHaveAttribute("aria-expanded", "false");
    expect(toggle()).toHaveAccessibleName("Ouvrir le menu");
    expect(drawer()).toHaveAttribute("data-open", "false");
  });

  it("mobile : ouvre le tiroir, rend le reste inerte et place le focus dedans", () => {
    stubViewport(true);
    mount();
    fireEvent.click(toggle());
    expect(drawer()).toHaveAttribute("data-open", "true");
    expect(toggle()).toHaveAttribute("aria-expanded", "true");
    expect(toggle()).toHaveAccessibleName("Fermer le menu");
    expect(document.querySelector("main")).toHaveAttribute("inert");
    expect(backdrop()).not.toHaveAttribute("inert");
    expect(drawer().contains(document.activeElement)).toBe(true);
    expect(document.documentElement.style.overflow).toBe("hidden");
  });

  it("Échap ferme et rend le focus au bouton menu, sans inert ni verrou résiduels", () => {
    stubViewport(true);
    mount();
    // Un clic réel donne le focus au bouton ; fireEvent.click ne le fait pas.
    toggle().focus();
    fireEvent.click(toggle());
    fireEvent.keyDown(document, { key: "Escape" });
    expect(drawer()).toHaveAttribute("data-open", "false");
    expect(document.activeElement).toBe(toggle());
    expect(document.querySelectorAll("[inert]")).toHaveLength(0);
    expect(document.documentElement.style.overflow).toBe("");
  });

  it("le voile et le bouton de fermeture du tiroir ferment", () => {
    stubViewport(true);
    mount();
    fireEvent.click(toggle());
    fireEvent.click(backdrop());
    expect(drawer()).toHaveAttribute("data-open", "false");
    fireEvent.click(toggle());
    const close = Array.from(drawer().querySelectorAll("button")).find(
      (b) => b.getAttribute("aria-label") === "Fermer le menu"
    ) as HTMLButtonElement;
    fireEvent.click(close);
    expect(drawer()).toHaveAttribute("data-open", "false");
  });

  it("suivre un lien de navigation referme le tiroir (liens existants conservés)", () => {
    stubViewport(true);
    mount();
    fireEvent.click(toggle());
    const link = screen.getByRole("link", { name: /Documents/ });
    expect(link).toHaveAttribute("href", "/documents");
    fireEvent.click(link);
    expect(drawer()).toHaveAttribute("data-open", "false");
  });

  it("un changement de page referme le tiroir", () => {
    stubViewport(true);
    const view = mount();
    fireEvent.click(toggle());
    nav.pathname = "/documents";
    act(() => {
      view.rerender(
        <AppShell>
          <Topbar title="Documents" />
        </AppShell>
      );
    });
    expect(drawer()).toHaveAttribute("data-open", "false");
  });

  it("desktop : aucun effet modal (ni inert, ni verrou de défilement)", () => {
    stubViewport(false);
    mount();
    fireEvent.click(toggle());
    expect(document.querySelectorAll("[inert]")).toHaveLength(0);
    expect(document.documentElement.style.overflow).toBe("");
  });
});
