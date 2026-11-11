import { describe, it, expect, vi } from "vitest";
import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { Composer, DraftComposer, type DraftComposerHandle } from "@/app/chat/_components/Composer";

function setup(overrides: Partial<React.ComponentProps<typeof Composer>> = {}) {
  const props = {
    input: "",
    setInput: vi.fn(),
    onSend: vi.fn(),
    onStop: vi.fn(),
    loading: false,
    streaming: false,
    canAsk: true,
    placeholder: "Posez une question…",
    ...overrides,
  };
  render(<Composer {...props} />);
  return props;
}

describe("Composer", () => {
  it("affiche le placeholder et le textarea", () => {
    setup();
    const ta = screen.getByLabelText("Votre question") as HTMLTextAreaElement;
    expect(ta).toBeInTheDocument();
    expect(ta.getAttribute("placeholder")).toBe("Posez une question…");
  });

  it("propage la saisie via setInput", () => {
    const { setInput } = setup();
    fireEvent.change(screen.getByLabelText("Votre question"), { target: { value: "bonjour" } });
    expect(setInput).toHaveBeenCalledWith("bonjour");
  });

  it("Entrée envoie, Maj+Entrée n'envoie pas", () => {
    const { onSend } = setup({ input: "ma question" });
    const ta = screen.getByLabelText("Votre question");
    fireEvent.keyDown(ta, { key: "Enter", shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(ta, { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("le bouton Envoyer est désactivé si le champ est vide", () => {
    setup({ input: "   " });
    expect(screen.getByRole("button", { name: /Envoyer/ })).toBeDisabled();
  });

  it("le bouton Envoyer est désactivé si canAsk est faux ou en chargement", () => {
    const { onSend } = setup({ input: "x", canAsk: false });
    const btn = screen.getByRole("button", { name: /Envoyer/ });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("le bouton Envoyer appelle onSend quand actif", () => {
    const { onSend } = setup({ input: "x" });
    fireEvent.click(screen.getByRole("button", { name: /Envoyer/ }));
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("le bouton Arrêter n'apparaît que pendant le stream et appelle onStop", () => {
    const { onStop } = setup({ input: "x", streaming: true, loading: true });
    const stop = screen.getByRole("button", { name: /Arrêter/ });
    fireEvent.click(stop);
    expect(onStop).toHaveBeenCalledTimes(1);
  });

  it("pas de bouton Arrêter hors stream", () => {
    setup({ streaming: false });
    expect(screen.queryByRole("button", { name: /Arrêter/ })).toBeNull();
  });
});

describe("DraftComposer — propriétaire du brouillon", () => {
  function setupDraft(overrides: Partial<React.ComponentProps<typeof DraftComposer>> = {}) {
    const ref = React.createRef<DraftComposerHandle>();
    const props = {
      onSend: vi.fn(),
      onStop: vi.fn(),
      loading: false,
      streaming: false,
      canAsk: true,
      placeholder: "Posez une question…",
      ...overrides,
    };
    const view = render(<DraftComposer ref={ref} {...props} />);
    return { ref, props, view };
  }
  const ta = () => screen.getByLabelText("Votre question") as HTMLTextAreaElement;

  it("onSend reçoit exactement le texte saisi, une seule fois", () => {
    const { props } = setupDraft();
    fireEvent.change(ta(), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: /Envoyer/ }));
    expect(props.onSend).toHaveBeenCalledTimes(1);
    expect(props.onSend).toHaveBeenCalledWith("abc");
  });

  it("Entrée envoie le texte saisi, Maj+Entrée non", () => {
    const { props } = setupDraft();
    fireEvent.change(ta(), { target: { value: "ligne" } });
    fireEvent.keyDown(ta(), { key: "Enter", shiftKey: true });
    expect(props.onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(ta(), { key: "Enter" });
    expect(props.onSend).toHaveBeenCalledWith("ligne");
  });

  it("n'envoie rien de lui-même : le vidage reste à la charge de la page", () => {
    setupDraft();
    fireEvent.change(ta(), { target: { value: "abc" } });
    fireEvent.click(screen.getByRole("button", { name: /Envoyer/ }));
    expect(ta().value).toBe("abc");
  });

  it("setValue remplace le brouillon (vidage, suggestion recopiée)", () => {
    const { ref } = setupDraft();
    fireEvent.change(ta(), { target: { value: "abc" } });
    act(() => ref.current!.setValue(""));
    expect(ta().value).toBe("");
    act(() => ref.current!.setValue("suggestion"));
    expect(ta().value).toBe("suggestion");
  });

  it("un rerendu du parent avec d'autres props conserve le brouillon", () => {
    const { props, view } = setupDraft();
    fireEvent.change(ta(), { target: { value: "en cours" } });
    view.rerender(<DraftComposer {...props} loading placeholder="Indexation…" onSend={vi.fn()} />);
    expect(ta().value).toBe("en cours");
    expect(screen.getByRole("button", { name: /Envoyer/ })).toBeDisabled();
  });

  it("le bouton Envoyer suit le brouillon local (vide ou espaces = désactivé)", () => {
    setupDraft();
    const send = () => screen.getByRole("button", { name: /Envoyer/ });
    expect(send()).toBeDisabled();
    fireEvent.change(ta(), { target: { value: "   " } });
    expect(send()).toBeDisabled();
    fireEvent.change(ta(), { target: { value: " x " } });
    expect(send()).not.toBeDisabled();
  });
});
