import "@/styles/tokens.css";
import type { Viewport } from "next";
import { Hanken_Grotesk } from "next/font/google";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";

const hanken = Hanken_Grotesk({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  variable: "--font-hanken",
  display: "swap",
});

// Viewport : `viewport-fit=cover` rend effectifs les env(safe-area-inset-*) du
// shell et du composer (iPhone à encoche / barre d'accueil) ; `interactive-widget`
// fait réduire le viewport dynamique (100dvh) par le clavier virtuel sur Android,
// pour que le composer de la page chat reste au-dessus du clavier.
export const viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  interactiveWidget: "resizes-content",
} satisfies Viewport;

// Métadonnées localisées : titre/description dépendent de la locale résolue.
export async function generateMetadata() {
  const t = await getTranslations("common");
  return {
    title: t("appTitle"),
    description: t("appDescription"),
  };
}

export default async function RootLayout({ children }: { children: React.ReactNode }) {
  // Locale résolue par next-intl (cookie > Accept-Language > fr). Valeur TOUJOURS
  // dans {fr, en} — jamais une valeur utilisateur brute.
  const locale = await getLocale();
  const messages = await getMessages();

  // Pas de wrapper centré ici : chaque écran gère sa propre mise en page
  // (connexion plein écran « split », application = sidebar + zone principale).
  return (
    <html lang={locale} className={hanken.variable}>
      <body>
        <NextIntlClientProvider locale={locale} messages={messages}>
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
