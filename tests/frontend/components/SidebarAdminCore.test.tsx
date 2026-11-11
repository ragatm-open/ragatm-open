import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";

// État mutable hoisté pour faire varier isGlobalOwner entre les tests.
const state = vi.hoisted(() => ({ isGlobalOwner: true }));

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
    isGlobalOwner: state.isGlobalOwner,
    logout: vi.fn(),
  }),
  ROLE_INFO: {
    admin: { labelKey: "administrator", descKey: "administratorDesc", color: "#000" },
    editeur: { labelKey: "contributor", descKey: "contributorDesc", color: "#000" },
    lecteur: { labelKey: "viewer", descKey: "viewerDesc", color: "#000" },
  },
}));

import { Sidebar } from "@/components/shell/Sidebar";

describe("Sidebar — entrée Admin Core", () => {
  beforeEach(() => {
    state.isGlobalOwner = true;
  });

  it("affiche le lien Admin Core pour le propriétaire global", () => {
    render(<Sidebar />);
    const link = screen.getByRole("link", { name: /Admin Core/ }) as HTMLAnchorElement;
    expect(link).toBeInTheDocument();
    expect(link.getAttribute("href")).toBe("/admin-core");
  });

  it("n'affiche pas le lien Admin Core pour un utilisateur non-owner", () => {
    state.isGlobalOwner = false;
    render(<Sidebar />);
    expect(screen.queryByRole("link", { name: /Admin Core/ })).toBeNull();
  });
});
