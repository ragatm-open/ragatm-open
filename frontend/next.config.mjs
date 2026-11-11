import createNextIntlPlugin from "next-intl/plugin";

// Pointe next-intl vers notre configuration de requête (mode sans routing localisé).
const withNextIntl = createNextIntlPlugin("./src/i18n/request.ts");

/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,

  // l'application n'utilise pas `next/image` ; l'API d'optimisation d'images
  // (`/_next/image`) est désactivée pour ne pas exposer un point d'entrée inutile
  // (avis de sécurité Next 14.x sur le traitement d'images). Aucun effet sur le rendu.
  images: { unoptimized: true },

  // IMPORTANT: empêche Next d'ajouter/enlever des slashes automatiquement
  trailingSlash: false,
  skipTrailingSlashRedirect: true,

  /**
   * Security headers
   * - Autorise l’embed PDF via blob: (PDF.js / iframe)
   * - Compatible avec CSP nginx existante
   */
  async headers() {
    return [
      {
        source: '/(.*)',
        headers: [
          {
            key: 'Content-Security-Policy',
            value: [
              "default-src 'self' https: data:",
              "frame-src 'self' blob: https:",
              "child-src 'self' blob: https:",
              "worker-src 'self' blob:",
              "img-src 'self' blob: data: https:",
              "script-src 'self' 'unsafe-inline' 'unsafe-eval' https:",
              "style-src 'self' 'unsafe-inline' https:",
            ].join('; '),
          },
        ],
      },
    ];
  },
};

export default withNextIntl(nextConfig);
