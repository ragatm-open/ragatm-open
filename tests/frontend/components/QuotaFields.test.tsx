/**
 * Champs de quota (nombre + taille avec unité) et jauge d'usage.
 * Vérifie : préremplissage, remontée en OCTETS, validation (entier > 0 / plafonds),
 * conversion d'unité, et affichage lecture seule de la consommation.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";

import { QuotaLimitsFields, QuotaUsageMeter } from "@/app/platform-admin/_components/QuotaFields";

const HARD_COUNT = 100000;
const HARD_BYTES = 1024 ** 4;

function renderFields(onChange = vi.fn()) {
  render(
    <QuotaLimitsFields
      initialCount={1000}
      initialBytes={10 * 1024 ** 3}
      hardMaxCount={HARD_COUNT}
      hardMaxBytes={HARD_BYTES}
      onChange={onChange}
    />
  );
  return onChange;
}

describe("QuotaLimitsFields", () => {
  it("préremplit et remonte les octets + validité", () => {
    const onChange = renderFields();
    expect(screen.getByDisplayValue("1000")).toBeTruthy();
    // Dernière remontée : 1000 fichiers, 10 Gio, valide.
    const last = onChange.mock.calls.at(-1)![0];
    expect(last).toEqual({ count: 1000, bytes: 10 * 1024 ** 3, valid: true });
  });

  it("invalide un nombre nul", () => {
    const onChange = renderFields();
    fireEvent.change(screen.getByDisplayValue("1000"), { target: { value: "0" } });
    const last = onChange.mock.calls.at(-1)![0];
    expect(last.valid).toBe(false);
  });

  it("invalide un dépassement du plafond", () => {
    const onChange = renderFields();
    fireEvent.change(screen.getByDisplayValue("1000"), { target: { value: String(HARD_COUNT + 1) } });
    expect(onChange.mock.calls.at(-1)![0].valid).toBe(false);
  });

  it("convertit selon l'unité choisie (octets canoniques)", () => {
    const onChange = renderFields();
    // Change la taille à 2 et l'unité à To.
    fireEvent.change(screen.getByDisplayValue("10"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("Unité de taille"), { target: { value: "TB" } });
    const last = onChange.mock.calls.at(-1)![0];
    expect(last.bytes).toBe(2 * 1024 ** 4);
  });
});

describe("QuotaUsageMeter", () => {
  it("affiche la consommation fichiers et stockage", () => {
    render(
      <QuotaUsageMeter
        usage={{
          max_document_count: 1000,
          used_document_count: 742,
          remaining_document_count: 258,
          max_document_bytes: 10 * 1024 ** 3,
          used_document_bytes: 5 * 1024 ** 3,
          remaining_document_bytes: 5 * 1024 ** 3,
          count_usage_percent: 74.2,
          size_usage_percent: 50,
          is_count_exceeded: false,
          is_size_exceeded: false,
          is_exceeded: false,
          can_upload: true,
          enforced: true,
        }}
      />
    );
    // Séparateur de milliers dépendant de la locale runtime : on normalise.
    expect(
      screen.getByText((c) => c.replace(/[\s, ]/g, "").includes("742/1000"))
    ).toBeTruthy();
    expect(screen.getByText(/5 Go \/ 10 Go/)).toBeTruthy();
  });
});
