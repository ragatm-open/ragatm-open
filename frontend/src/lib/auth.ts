// Authentification : le token DRF vit désormais dans un cookie HttpOnly posé
// par le backend, INACCESSIBLE au JavaScript. On ne stocke plus aucun token en
// localStorage / sessionStorage / cookie lisible.
//
// La source de vérité "connecté ou non" est l'appel GET /api/v1/auth/me/.
// Ce module n'expose plus que le helper CSRF (cookie `csrftoken`, NON sensible :
// c'est le pendant "double-submit" du token d'auth, pas un secret de session).

const CSRF_COOKIE = "csrftoken";

/** Lit le cookie CSRF (non sensible) pour le renvoyer en header X-CSRFToken. */
export function getCsrfToken(): string | null {
  if (typeof document === "undefined") return null;
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${CSRF_COOKIE}=([^;]*)`));
  return match ? decodeURIComponent(match[1]) : null;
}
