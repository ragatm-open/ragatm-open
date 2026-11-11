"use client";

// Traduction locale des erreurs à partir d'un CODE stable (doctrine mission i18n,
// §8). Le backend renvoie un statut HTTP (et parfois un `detail`) ; on le
// normalise en un code métier, puis le frontend traduit `errors.<CODE>`. Le
// message brut du backend n'est JAMAIS affiché pour une erreur connue : réservé
// aux logs / diagnostic. En dernier recours, repli sur `errors.fallback`.

import { useCallback } from "react";
import { useTranslations } from "next-intl";

import { ApiError, isApiError } from "./apiError";

// Codes reconnus (miroir du namespace `errors`).
export type ErrorCode =
  | "INVALID_CREDENTIALS"
  | "SESSION_EXPIRED"
  | "PERMISSION_DENIED"
  | "RESOURCE_NOT_FOUND"
  | "DOCUMENT_NOT_FOUND"
  | "DOCUMENT_UPLOAD_FAILED"
  | "DOCUMENT_FORMAT_UNSUPPORTED"
  | "DOCUMENT_TOO_LARGE"
  | "DOCUMENT_INGESTION_FAILED"
  | "WORKSPACE_NOT_FOUND"
  | "RAGSPACE_NOT_FOUND"
  | "INVITATION_INVALID"
  | "INVITATION_EXPIRED"
  | "NETWORK_ERROR"
  | "SERVICE_UNAVAILABLE"
  | "AI_SERVICE_UNAVAILABLE"
  | "VALIDATION_ERROR"
  | "UNKNOWN_ERROR";

/** Déduit un code d'erreur stable depuis une erreur inconnue (statut HTTP + indices). */
export function errorCode(e: unknown): ErrorCode {
  if (!isApiError(e)) {
    // Erreur non-HTTP : très probablement réseau / abort.
    return "NETWORK_ERROR";
  }
  const err = e as ApiError;
  const status = err.status;
  switch (status) {
    case 400:
      return "VALIDATION_ERROR";
    case 401:
      return "SESSION_EXPIRED";
    case 403:
      return "PERMISSION_DENIED";
    case 404:
      return "RESOURCE_NOT_FOUND";
    case 413:
      return "DOCUMENT_TOO_LARGE";
    case 415:
      return "DOCUMENT_FORMAT_UNSUPPORTED";
    case 502:
    case 503:
    case 504:
      return "SERVICE_UNAVAILABLE";
    default:
      return "UNKNOWN_ERROR";
  }
}

/**
 * Hook renvoyant un traducteur d'erreurs : `(error) => string`. Utilise le
 * namespace `errors` ; repli sur `errors.fallback` pour tout code inattendu.
 */
export function useErrorMessage() {
  const t = useTranslations("errors");
  return useCallback(
    (e: unknown, fallbackCode: ErrorCode = "UNKNOWN_ERROR"): string => {
      const code = isApiError(e) || !e ? errorCode(e) : fallbackCode;
      try {
        return t(code);
      } catch {
        return t("fallback");
      }
    },
    [t]
  );
}
