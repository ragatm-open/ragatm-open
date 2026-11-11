// frontend/src/app/accept-invitation/page.tsx
import { Suspense } from "react";
import { getTranslations } from "next-intl/server";
import AcceptInvitationClient from "./AcceptInvitationClient";

// Politique de référent : ne jamais fuiter le token via l'en-tête Referer tant
// qu'il est dans l'URL. Rendu en <meta name="referrer" content="no-referrer">.
export async function generateMetadata() {
  const t = await getTranslations("invitations");
  return {
    title: t("joinTitle", { workspace: "RAGatm" }),
    referrer: "no-referrer" as const,
    robots: { index: false, follow: false },
  };
}

// Page publique d'acceptation d'invitation. Le token arrive dans la query string
// (lien email / copié). La page le valide côté serveur puis oriente le flux.
export default async function AcceptInvitationPage() {
  const tc = await getTranslations("common");
  return (
    <Suspense fallback={<div style={{ padding: 24 }}>{tc("loading")}</div>}>
      <AcceptInvitationClient />
    </Suspense>
  );
}
