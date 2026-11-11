// Configuration de requête next-intl (mode SANS routing localisé : aucune URL
// n'est préfixée par la langue). La locale active est résolue par requête à
// partir du cookie `ragatm_locale`, puis de l'en-tête `Accept-Language`, puis
// du français par défaut (cf. `resolveLocale`).

import { getRequestConfig } from "next-intl/server";
import { cookies, headers } from "next/headers";

import { LOCALE_COOKIE } from "./config";
import { resolveLocale } from "./locale";
import { loadMessages } from "./messages";

export default getRequestConfig(async () => {
  // Next 15 : `cookies()` et `headers()` sont asynchrones (API dynamiques).
  const cookieStore = await cookies();
  const headerStore = await headers();

  const locale = resolveLocale({
    cookie: cookieStore.get(LOCALE_COOKIE)?.value,
    acceptLanguage: headerStore.get("accept-language"),
  });

  return {
    locale,
    messages: await loadMessages(locale),
    // Fuseau/heure : formatage cohérent serveur/client. La locale pilote les
    // formats via next-intl (dates, nombres, pluriels ICU).
    now: new Date(),
  };
});
