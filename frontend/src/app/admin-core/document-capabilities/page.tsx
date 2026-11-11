"use client";

import React from "react";
import { AppShell } from "@/components/shell";
import { DocumentCapabilitiesScreen } from "./_components/DocumentCapabilitiesScreen";

/** « Capacités documentaires » — superuser seulement, LECTURE SEULE. */
export default function DocumentCapabilitiesPage() {
  return (
    <AppShell>
      <DocumentCapabilitiesScreen />
    </AppShell>
  );
}
