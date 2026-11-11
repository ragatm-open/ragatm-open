"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams, useRouter } from "next/navigation";
import { useTranslations } from "next-intl";

import { apiGet } from "@/lib/api";
import { DocumentToolbar } from "./_components/DocumentToolbar";
import { DocumentSnippet } from "./_components/DocumentSnippet";
import { PdfDocumentViewer } from "./_components/PdfDocumentViewer";
import { UnsupportedDocumentViewer } from "./_components/UnsupportedDocumentViewer";
import { SpreadsheetDocumentViewer } from "./_components/SpreadsheetDocumentViewer";
import { DocumentPreviewViewer } from "./_components/DocumentPreviewViewer";
import { PresentationPreviewViewer } from "./_components/PresentationPreviewViewer";
import { resolvePreviewKind, type PreviewKind } from "./_lib/previewKind";

function safeInt(v: string | null): number | null {
  const n = Number(v || "");
  return Number.isFinite(n) && n > 0 ? n : null;
}

type DocumentMeta = {
  id: string;
  filename?: string | null;
  content_type?: string | null;
  preview_kind?: string | null;
  sha256?: string | null;
};

/**
 * Routeur de visualisation.
 *
 * Avant ce lot, la page téléchargeait TOUT document en Blob et le posait dans une
 * `<iframe>` en annonçant un `Accept` PDF. Un `.xlsx` y produisait une zone blanche :
 * aucun navigateur ne rend nativement ce type MIME.
 *
 * Désormais la page interroge d'abord les métadonnées du document (une requête JSON
 * légère, sur un endpoint qui existait déjà), lit le `preview_kind` décidé par le
 * backend, et choisit le visualiseur. Aucun octet du fichier n'est téléchargé avant
 * que le type ne soit connu.
 */
export default function DocumentViewerPage() {
  const t = useTranslations("documentViewer");
  const router = useRouter();
  const params = useParams<{ documentId: string }>();
  const search = useSearchParams();

  const documentId = params?.documentId;

  const page = useMemo(() => safeInt(search.get("page")), [search]);
  const snippet = useMemo(() => (search.get("snippet") || "").trim(), [search]);
  const back = useMemo(() => (search.get("back") || "/chat").trim() || "/chat", [search]);
  // Consommé par le visualiseur tabulaire : c'est LUI, et non le texte du snippet,
  // qui fait autorité sur la localisation de la source.
  const chunkId = useMemo(() => (search.get("chunk_id") || "").trim(), [search]);

  const [doc, setDoc] = useState<DocumentMeta | null>(null);
  const [metaErr, setMetaErr] = useState<string | null>(null);
  const [openHref, setOpenHref] = useState<string | null>(null);
  // Localisation remontée par le visualiseur tabulaire, pour la barre d'outils.
  const [location, setLocation] = useState<{ sheetName: string | null; ranges: string[] }>({
    sheetName: null,
    ranges: [],
  });

  const downloadHref = useMemo(() => {
    if (!documentId) return "#";
    return `/api/v1/documents/${encodeURIComponent(documentId)}/download/?download=1`;
  }, [documentId]);

  const onUnauthorized = useCallback(() => router.replace("/login"), [router]);
  const onBack = useCallback(() => router.push(back), [router, back]);
  const onDownload = useCallback(
    () => window.open(downloadHref, "_blank", "noreferrer"),
    [downloadHref]
  );

  // `onUnauthorized` dérive de `useRouter()`. Le placer dans les dépendances de
  // l'effet le lierait à l'identité de l'objet router : si celle-ci n'est pas stable
  // d'un rendu à l'autre, l'effet se relance sans fin et l'état ne se pose jamais
  // (l'écran reste bloqué sur « Chargement… »). On passe donc par une ref : le
  // chargement des métadonnées ne dépend que du document demandé.
  const onUnauthorizedRef = useRef(onUnauthorized);
  useEffect(() => {
    onUnauthorizedRef.current = onUnauthorized;
  }, [onUnauthorized]);

  useEffect(() => {
    if (!documentId) return;
    let cancelled = false;

    (async () => {
      setMetaErr(null);
      setDoc(null);
      try {
        const d = await apiGet<DocumentMeta>(
          `/api/v1/documents/${encodeURIComponent(documentId)}/`
        );
        if (!cancelled) setDoc(d);
      } catch (e: unknown) {
        const msg = String((e as { message?: string })?.message || "");
        if (/\b401\b/.test(msg)) {
          onUnauthorizedRef.current();
          return;
        }
        if (!cancelled) setMetaErr(msg || t("error"));
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [documentId]);

  const title =
    doc?.filename ||
    (documentId ? t("documentNumbered", { id: documentId }) : t("documentTitle"));
  const kind: PreviewKind | null = doc ? resolvePreviewKind(doc) : null;

  const shell = (children: React.ReactNode, fill: boolean) => (
    <div style={{ minHeight: "100vh", padding: 12, background: "#f3f4f6" }}>
      <div style={{ maxWidth: 1200, margin: "0 auto" }}>
        <DocumentToolbar
          title={title}
          page={kind === "pdf" ? page : null}
          sheetName={location.sheetName}
          cellRanges={location.ranges}
          onBack={onBack}
          onDownload={onDownload}
          openHref={openHref}
        />

        {snippet ? <DocumentSnippet snippet={snippet} /> : null}

        <div style={{ marginTop: 12, height: fill ? "calc(100vh - 140px)" : undefined }}>
          {children}
        </div>
      </div>
    </div>
  );

  // Erreur de métadonnées : jamais une zone blanche — un état lisible et actionnable.
  if (metaErr) {
    const notFound = /\b404\b/.test(metaErr);
    return shell(
      <UnsupportedDocumentViewer
        filename={doc?.filename}
        reason={
          notFound
            ? t("notFoundOrNoAccess")
            : t("metaLoadError", { detail: metaErr })
        }
        onDownload={onDownload}
        onBack={onBack}
      />,
      false
    );
  }

  if (!doc || !kind) {
    return shell(
      <div style={{ padding: 16, opacity: 0.8 }} aria-live="polite">
        {t("loadingDocument")}
      </div>,
      false
    );
  }

  if (kind === "pdf") {
    return shell(
      <PdfDocumentViewer
        documentId={doc.id}
        page={page}
        onUnauthorized={onUnauthorized}
        onOpenHrefChange={setOpenHref}
      />,
      true
    );
  }

  if (kind === "spreadsheet") {
    return shell(
      <SpreadsheetDocumentViewer
        documentId={doc.id}
        filename={doc.filename || ""}
        chunkId={chunkId || null}
        onUnauthorized={onUnauthorized}
        onDownload={onDownload}
        onBack={onBack}
        onLocationChange={setLocation}
      />,
      false
    );
  }

  if (kind === "document") {
    return shell(
      <DocumentPreviewViewer
        documentId={doc.id}
        filename={doc.filename || ""}
        chunkId={chunkId || null}
        onUnauthorized={onUnauthorized}
        onDownload={onDownload}
        onBack={onBack}
      />,
      false
    );
  }

  if (kind === "presentation") {
    return shell(
      <PresentationPreviewViewer
        documentId={doc.id}
        filename={doc.filename || ""}
        chunkId={chunkId || null}
        onUnauthorized={onUnauthorized}
        onDownload={onDownload}
        onBack={onBack}
      />,
      false
    );
  }

  // `text` n'a pas de visualiseur dédié dans ce lot : il retombe sur l'état
  // explicite plutôt que sur une iframe au comportement dépendant du navigateur.
  return shell(
    <UnsupportedDocumentViewer
      filename={doc.filename}
      reason={
        kind === "text" ? t("textPreviewUnavailable") : undefined
      }
      page={page}
      onDownload={onDownload}
      onBack={onBack}
    />,
    false
  );
}
