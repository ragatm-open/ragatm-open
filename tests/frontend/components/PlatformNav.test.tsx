import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// isSuperuser mutable pour vérifier le gating de l'entrée « Administration plateforme ».
const state = vi.hoisted(() => ({ isSuperuser: true }));

vi.mock("next/navigation", () => ({
  usePathname: () => "/home",
  // Le Sidebar rend désormais <LanguageSelector>, qui utilise useRouter().refresh().
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
vi.mock("@/components/shell/WorkspaceContext", () => ({
  useWorkspace: () => ({
    me: { username: "root", email: "root@e.com" },
    role: "admin",
    isAdmin: true,
    isGlobalOwner: false,
    isSuperuser: state.isSuperuser,
    logout: vi.fn(),
  }),
  ROLE_INFO: {
    admin: { labelKey: "administrator", descKey: "administratorDesc", color: "#000" },
    editeur: { labelKey: "contributor", descKey: "contributorDesc", color: "#000" },
    lecteur: { labelKey: "viewer", descKey: "viewerDesc", color: "#000" },
  },
}));

import { Sidebar } from "@/components/shell/Sidebar";

describe("Sidebar — entrée Administration plateforme (superuser)", () => {
  beforeEach(() => {
    state.isSuperuser = true;
  });

  it("affiche le lien pour un superuser", () => {
    render(<Sidebar />);
    const link = screen.getByRole("link", { name: /Administration plateforme/ }) as HTMLAnchorElement;
    expect(link).toBeInTheDocument();
    expect(link.getAttribute("href")).toBe("/platform-admin");
  });

  it("masque le lien pour un non-superuser (même admin/owner de workspace)", () => {
    state.isSuperuser = false;
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: /Administration plateforme/ })).toBeNull();
  });
});
