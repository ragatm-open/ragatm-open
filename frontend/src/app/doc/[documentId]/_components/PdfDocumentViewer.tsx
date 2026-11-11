"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";

import { apiFetchBlobUrl } from "@/lib/api";

/**
 * Visualiseur PDF — comportement HISTORIQUE, extrait tel quel de `page.tsx`.
 *
 * Ce composant est volontairement un déplacement SANS changement de logique :
 * téléchargement en Blob, `URL.createObjectURL`, ancre `#page=N`, rendu en
 * `<iframe>`, révocation au démontage. Le seul écart avec l'ancienne page est
 * l'en-tête `Accept`, qui n'annonce plus `application/pdf` avant que le type du
 * document ne soit connu (il l'est désormais : ce composant n'est monté QUE pour
 * un document dont `preview_kind === "pdf"`).
 */
export function PdfDocumentViewer(props: {
  documentId: string;
  page: number | null;
  onUnauthorized: () => void;
  /**
   * Remonte l'URL du blob (ancre `#page=N` incluse) pour que la barre d'outils
   * conserve son bouton « Ouvrir ↗ ». Sans ce rappel, extraire le viewer dans son
   * propre composant aurait fait disparaître le bouton — une régression PDF.
   */
  onOpenHrefChange?: (href: string | null) => void;
}) {
  const { documentId, page, onUnauthorized, onOpenHrefChange } = props;
  const t = useTranslations("documentViewer");

  const [err, setErr] = useState<string | null>(null);
  const [blobUrl, setBlobUrl] = useState<string | null>(null);

  // Rappels gardés en ref : les mettre dans les dépendances lierait le
  // téléchargement à leur identité, et un rappel recréé à chaque rendu relancerait
  // la requête sans fin (en révoquant à chaque fois le blob précédent).
  const cbRef = useRef({ onUnauthorized, onOpenHrefChange });
  useEffect(() => {
    cbRef.current = { onUnauthorized, onOpenHrefChange };
  }, [onUnauthorized, onOpenHrefChange]);

  useEffect(() => {
    if (!documentId) return;

    let revoke: null | (() => void) = null;
    let cancelled = false;

    async function run() {
      setErr(null);
      setBlobUrl(null);

      try {
        // Auth via cookie HttpOnly (credentials inclus par apiFetchBlobUrl).
        const { url, revoke: r } = await apiFetchBlobUrl(
          `/api/v1/documents/${encodeURIComponent(documentId)}/download/`,
          { method: "GET", headers: { Accept: "application/pdf,*/*" } }
        );

        revoke = r;
        if (cancelled) {
          r();
          return;
        }
        const href = page ? `${url}#page=${page}` : url;
        setBlobUrl(href);
        cbRef.current.onOpenHrefChange?.(href);
      } catch (e: unknown) {
        const msg = String((e as { message?: string })?.message || "");
        if (/\b401\b/.test(msg)) {
          cbRef.current.onUnauthorized();
          return;
        }
        if (!cancelled) setErr(msg || t("error"));
      }
    }

    run();

    return () => {
      cancelled = true;
      cbRef.current.onOpenHrefChange?.(null);
      try {
        if (revoke) revoke();
      } catch {
        // ignore
      }
    };
  }, [documentId, page]);

  if (err) {
    return (
      <div
        role="alert"
        style={{
          padding: 16,
          border: "1px solid #fecaca",
          borderRadius: 12,
          background: "#fef2f2",
          color: "#b91c1c",
          whiteSpace: "pre-wrap",
        }}
      >
        {err}
      </div>
    );
  }

  if (!blobUrl) {
    return (
      <div style={{ padding: 16, opacity: 0.8 }} aria-live="polite">
        {t("loadingDocument")}
      </div>
    );
  }

  return (
    <iframe
      title={t("documentTitle")}
      src={blobUrl}
      style={{
        width: "100%",
        height: "100%",
        border: "1px solid #e5e7eb",
        borderRadius: 12,
        background: "white",
      }}
    />
  );
}
