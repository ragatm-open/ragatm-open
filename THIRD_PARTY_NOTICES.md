# Mentions de tiers — RAGatm Open (backend, frontend, kit Compact)

Ce fichier recense les composants tiers utilisés, référencés ou redistribués par cette distribution, avec les licences publiées par leurs éditeurs (métadonnées de paquets, fiches de modèles). Il ne constitue pas une analyse juridique. Le code original de ce dépôt est sous **Apache-2.0** (`LICENSE`, `NOTICE`) ; les composants ci-dessous conservent leurs licences respectives.

## Composants soumis à une obligation particulière

| Composant | Licence | Où | Obligation / disposition prise |
|---|---|---|---|
| psycopg / psycopg-binary 3.1.x | LGPL-3.0 | backend (cœur) | bibliothèque utilisée telle quelle par liaison dynamique, non modifiée ; sources : https://github.com/psycopg/psycopg |
| ifcopenshell 0.8.x | LGPL-3.0-or-later | backend, extra optionnel `ifc` (jamais dans l'image par défaut) | si l'image `WITH_IFC=1` est redistribuée : conserver cette mention et le lien vers les sources https://github.com/IfcOpenShell/IfcOpenShell |
| certifi | MPL-2.0 | backend, frontend (via outils), Gateway, Pool | fichier de certificats non modifié ; sources : https://github.com/certifi/python-certifi |
| pathspec | MPL-2.0 | outillage de développement | non modifié |
| libvips / `@img/sharp-libvips-*`, `@img/sharp-*` | LGPL-3.0-or-later (libvips) ; Apache-2.0 (sharp) | frontend : dépendances optionnelles de `next` présentes dans `node_modules` de l'image ; l'optimisation d'images est désactivée (`images.unoptimized`) | binaires non modifiés ; mention conservée ; sources : https://github.com/libvips/libvips, https://github.com/lovell/sharp |
| caniuse-lite | CC-BY-4.0 | frontend (données de compatibilité navigateurs) | attribution : « Browserslist / caniuse-lite, données de caniuse.com, CC-BY-4.0 » |
| GNU LibreDWG 0.14 (`dwg2dxf`) | GPL-3.0-or-later | backend : étape de construction **optionnelle** `WITH_DWG=1` (désactivée en Compact et dans les images par défaut) | exécutable séparé appelé en sous-processus ; si une image `WITH_DWG=1` est redistribuée : fournir `COPYING` et les sources (ou offre écrite) de LibreDWG |
| PyMuPDF | AGPL-3.0 **ou** licence commerciale Artifex | backend : extra optionnel `pdf-mupdf`, **jamais installé par défaut** (garde automatisée) | l'activation engage l'AGPL-3.0 pour le déploiement concerné ; non redistribué |
| Modèles (`granite-embedding:278m`, `qwen2.5:*-instruct`, `BAAI/bge-reranker-v2-m3`) | Apache-2.0 | installés par l'exploitant, **aucun poids redistribué** | respecter les fiches de modèles |
| Images tierces (`python:3.12-slim`, `node:22-alpine`, `pgvector/pgvector:pg16`, `redis:7.2-alpine`, `ollama/ollama`, `ghcr.io/huggingface/text-embeddings-inference`) | PSF / MIT / PostgreSQL License / BSD-3-Clause / MIT / Apache-2.0 (+ licences des distributions Debian et Alpine) | kit Compact (référencées par digest, jamais rebrandées) | aucune redistribution par ce dépôt |

## Bibliothèques Python du backend (`backend/requirements.lock` = runtime ; `ci/requirements-ci.lock` = développement/CI)

| Paquet | Version | Licence (métadonnées publiées) | Portée |
|---|---|---|---|
| amqp | 5.4.2 | BSD | runtime |
| annotated-types | 0.8.0 | MIT | runtime |
| asgiref | 3.13.0 | BSD-3-Clause | runtime |
| billiard | 4.3.2 | BSD | runtime |
| black | 24.10.0 | MIT | développement / CI |
| celery | 5.6.3 | BSD-3-Clause | runtime |
| certifi | 2026.10.23 | MPL-2.0 | runtime |
| cffi | 2.1.1 | MIT-0 | runtime |
| charset-normalizer | 3.5.2 | MIT | runtime |
| click | 8.5.1 | BSD-3-Clause | runtime |
| click-didyoumean | 0.3.1 | MIT | runtime |
| click-plugins | 1.1.1.2 | New BSD | runtime |
| click-repl | 0.4.1 | MIT | runtime |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause | runtime |
| defusedxml | 0.7.1 | PSFL | runtime |
| Django | 5.2.19 | BSD-3-Clause | runtime |
| django-axes | 8.3.1 | MIT | runtime |
| django-otp | 1.7.3 | Unlicense | runtime |
| django-redis | 5.4.0 | BSD-3-Clause | runtime |
| djangorestframework | 3.18.3 | BSD-3-Clause | runtime |
| duckdb | 1.5.6 | MIT License | runtime |
| et_xmlfile | 2.0.0 | MIT | runtime |
| execnet | 2.1.2 | MIT | développement / CI |
| ezdxf | 1.4.4 | MIT License | runtime |
| fonttools | 4.68.0 | MIT | runtime |
| idna | 3.20 | BSD-3-Clause | runtime |
| ifcopenshell | 0.8.5 | GNU Lesser General Public License v3 or later (LGPLv3+) | développement / CI |
| iniconfig | 2.3.1 | MIT | développement / CI |
| isodate | 0.7.2 | Copyright (c) 2021, Hugo van Kemenade and contributors | développement / CI |
| kombu | 5.6.2 | BSD-3-Clause | runtime |
| lark | 1.3.1 | MIT | développement / CI |
| lxml | 6.1.3 | BSD-3-Clause | runtime |
| mypy_extensions | 1.2.0 | MIT | développement / CI |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | runtime |
| openpyxl | 3.1.5 | MIT | runtime |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | runtime |
| pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | développement / CI |
| pdfminer.six | 20251230 | MIT | développement / CI |
| pdfplumber | 0.11.9 | MIT License | runtime |
| pgvector | 0.3.6 | MIT | runtime |
| pillow | 13.0.0 | MIT-CMU | runtime |
| platformdirs | 4.14.5 | MIT | développement / CI |
| pluggy | 1.7.0 | MIT | développement / CI |
| prompt_toolkit | 3.0.53 | BSD License | runtime |
| psycopg | 3.1.20 | GNU Lesser General Public License v3 (LGPLv3) | runtime |
| psycopg-binary | 3.1.20 | GNU Lesser General Public License v3 (LGPLv3) | runtime |
| pycparser | 3.12 | BSD-3-Clause | runtime |
| pydantic | 2.14.3 | MIT | runtime |
| pydantic_core | 2.50.4 | MIT | runtime |
| Pygments | 2.21.0 | BSD-2-Clause | développement / CI |
| PyJWT | 2.15.1 | MIT | runtime |
| pyparsing | 3.3.3 | MIT | runtime |
| pypdf | 6.21.4 | BSD-3-Clause | runtime |
| pypdfium2 | 4.30.0 | (Apache-2.0 OR BSD-3-Clause) AND LicenseRef-PdfiumThirdParty | runtime |
| pytesseract | 0.3.13 | Apache License 2.0 | développement / CI |
| pytest | 8.4.2 | MIT | développement / CI |
| pytest-django | 4.14.0 | pytest-django is released under the BSD (3-clause) license | développement / CI |
| pytest-xdist | 3.8.0 | MIT | développement / CI |
| python-dateutil | 4.0.0 | BSD-3-Clause | runtime |
| python-dotenv | 1.2.4 | BSD-3-Clause | runtime |
| python-pptx | 1.0.2 | MIT | runtime |
| PyYAML | 6.0.3 | MIT | développement / CI |
| RapidFuzz | 3.14.6 | MIT | runtime |
| redis | 5.3.1 | MIT | runtime |
| reportlab | 4.5.1 | BSD license (see license.txt for details), Copyright (c) 200 | développement / CI |
| requests | 2.34.2 | Apache-2.0 | runtime |
| ruff | 0.4.10 | MIT | développement / CI |
| shapely | 2.2.0 | BSD-3-Clause | développement / CI |
| sqlparse | 0.6.1 | BSD License | runtime |
| typing-inspection | 0.4.5 | MIT | runtime |
| typing_extensions | 4.16.0 | PSF-2.0 | runtime |
| tzdata | 2026.6 | Apache-2.0 | runtime |
| tzlocal | 5.4.5 | MIT | runtime |
| urllib3 | 2.8.0 | MIT | runtime |
| vine | 5.1.0 | BSD | runtime |
| wcwidth | 0.9.4 | MIT License | runtime |
| xlsxwriter | 3.3.1 | BSD-2-Clause | runtime |

## Paquets npm du frontend — dépendances de production (`frontend/package-lock.json`)

| Paquet | Version | Licence (package-lock) |
|---|---|---|
| @emnapi/runtime | 1.11.3 | MIT |
| @formatjs/fast-memoize | 3.1.7 | MIT |
| @formatjs/icu-messageformat-parser | 3.5.15 | MIT |
| @formatjs/icu-skeleton-parser | 2.1.11 | MIT |
| @formatjs/intl-localematcher | 0.8.13 | MIT |
| @img/colour | 1.1.0 | MIT |
| @img/sharp-darwin-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-darwin-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-freebsd-wasm32 | 0.35.5 | Apache-2.0 |
| @img/sharp-libvips-darwin-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-darwin-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-ppc64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-riscv64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-s390x | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linux-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-arm64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-libvips-linuxmusl-x64 | 1.3.4 | LGPL-3.0-or-later |
| @img/sharp-linux-arm | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-ppc64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-riscv64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-s390x | 0.35.5 | Apache-2.0 |
| @img/sharp-linux-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linuxmusl-arm64 | 0.35.5 | Apache-2.0 |
| @img/sharp-linuxmusl-x64 | 0.35.5 | Apache-2.0 |
| @img/sharp-wasm32 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later AND MIT |
| @img/sharp-webcontainers-wasm32 | 0.35.5 | Apache-2.0 |
| @img/sharp-win32-arm64 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-ia32 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @img/sharp-win32-x64 | 0.35.5 | Apache-2.0 AND LGPL-3.0-or-later |
| @next/env | 15.5.29 | MIT |
| @next/swc-darwin-arm64 | 15.5.29 | MIT |
| @next/swc-darwin-x64 | 15.5.29 | MIT |
| @next/swc-linux-arm64-gnu | 15.5.29 | MIT |
| @next/swc-linux-arm64-musl | 15.5.29 | MIT |
| @next/swc-linux-x64-gnu | 15.5.29 | MIT |
| @next/swc-linux-x64-musl | 15.5.29 | MIT |
| @next/swc-win32-arm64-msvc | 15.5.29 | MIT |
| @next/swc-win32-x64-msvc | 15.5.29 | MIT |
| @parcel/watcher | 2.6.0 | MIT |
| @parcel/watcher-android-arm64 | 2.6.0 | MIT |
| @parcel/watcher-darwin-arm64 | 2.6.0 | MIT |
| @parcel/watcher-darwin-x64 | 2.6.0 | MIT |
| @parcel/watcher-freebsd-x64 | 2.6.0 | MIT |
| @parcel/watcher-linux-arm-glibc | 2.6.0 | MIT |
| @parcel/watcher-linux-arm-musl | 2.6.0 | MIT |
| @parcel/watcher-linux-arm64-glibc | 2.6.0 | MIT |
| @parcel/watcher-linux-arm64-musl | 2.6.0 | MIT |
| @parcel/watcher-linux-x64-glibc | 2.6.0 | MIT |
| @parcel/watcher-linux-x64-musl | 2.6.0 | MIT |
| @parcel/watcher-win32-arm64 | 2.6.0 | MIT |
| @parcel/watcher-win32-x64 | 2.6.0 | MIT |
| @schummar/icu-type-parser | 1.21.5 | MIT |
| @swc/core | 1.15.46 | Apache-2.0 |
| @swc/core-darwin-arm64 | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-darwin-x64 | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-linux-arm-gnueabihf | 1.15.46 | Apache-2.0 |
| @swc/core-linux-arm64-gnu | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-linux-arm64-musl | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-linux-ppc64-gnu | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-linux-s390x-gnu | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-linux-x64-gnu | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-linux-x64-musl | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-win32-arm64-msvc | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-win32-ia32-msvc | 1.15.46 | Apache-2.0 AND MIT |
| @swc/core-win32-x64-msvc | 1.15.46 | Apache-2.0 AND MIT |
| @swc/counter | 0.1.3 | voir le paquet |
| @swc/helpers | 0.5.5 | voir le paquet |
| @swc/types | 0.1.27 | Apache-2.0 |
| @types/debug | 4.1.13 | MIT |
| @types/estree | 1.0.10 | MIT |
| @types/estree-jsx | 1.0.5 | MIT |
| @types/hast | 3.0.5 | MIT |
| @types/mdast | 4.0.4 | MIT |
| @types/ms | 2.1.0 | MIT |
| @types/unist | 2.0.11 | MIT |
| @types/unist | 3.0.3 | MIT |
| @ungap/structured-clone | 1.3.3 | ISC |
| ansi-regex | 5.0.1 | MIT |
| ansi-styles | 4.3.0 | MIT |
| bail | 2.0.2 | MIT |
| camelcase | 5.3.1 | MIT |
| caniuse-lite | 1.0.30001823 | CC-BY-4.0 |
| ccount | 2.0.1 | MIT |
| character-entities | 2.0.2 | MIT |
| character-entities-html4 | 2.1.0 | MIT |
| character-entities-legacy | 3.0.0 | MIT |
| character-reference-invalid | 2.0.1 | MIT |
| client-only | 0.0.1 | MIT |
| cliui | 6.0.0 | ISC |
| color-convert | 2.0.1 | MIT |
| color-name | 1.1.4 | MIT |
| comma-separated-tokens | 2.0.3 | MIT |
| debug | 4.4.3 | MIT |
| decamelize | 1.2.0 | MIT |
| decode-named-character-reference | 1.3.0 | MIT |
| dequal | 2.0.3 | MIT |
| detect-libc | 2.1.2 | Apache-2.0 |
| devlop | 1.1.0 | MIT |
| dijkstrajs | 1.0.3 | MIT |
| emoji-regex | 8.0.0 | MIT |
| escape-string-regexp | 5.0.0 | MIT |
| estree-util-is-identifier-name | 3.0.0 | MIT |
| extend | 3.0.2 | MIT |
| find-up | 4.1.0 | MIT |
| get-caller-file | 2.0.5 | ISC |
| hast-util-to-jsx-runtime | 2.3.6 | MIT |
| hast-util-whitespace | 3.0.0 | MIT |
| html-url-attributes | 3.0.1 | MIT |
| icu-minify | 4.13.4 | MIT |
| inline-style-parser | 0.2.7 | MIT |
| intl-messageformat | 11.2.12 | BSD-3-Clause |
| is-alphabetical | 2.0.1 | MIT |
| is-alphanumerical | 2.0.1 | MIT |
| is-decimal | 2.0.1 | MIT |
| is-extglob | 2.1.1 | MIT |
| is-fullwidth-code-point | 3.0.0 | MIT |
| is-glob | 4.0.3 | MIT |
| is-hexadecimal | 2.0.1 | MIT |
| is-plain-obj | 4.1.0 | MIT |
| locate-path | 5.0.0 | MIT |
| longest-streak | 3.1.0 | MIT |
| markdown-table | 3.0.4 | MIT |
| mdast-util-find-and-replace | 3.0.2 | MIT |
| mdast-util-from-markdown | 2.0.3 | MIT |
| mdast-util-gfm | 3.1.0 | MIT |
| mdast-util-gfm-autolink-literal | 2.0.1 | MIT |
| mdast-util-gfm-footnote | 2.1.0 | MIT |
| mdast-util-gfm-strikethrough | 2.0.0 | MIT |
| mdast-util-gfm-table | 2.0.0 | MIT |
| mdast-util-gfm-task-list-item | 2.0.0 | MIT |
| mdast-util-mdx-expression | 2.0.1 | MIT |
| mdast-util-mdx-jsx | 3.2.0 | MIT |
| mdast-util-mdxjs-esm | 2.0.1 | MIT |
| mdast-util-phrasing | 4.1.0 | MIT |
| mdast-util-to-hast | 13.2.1 | MIT |
| mdast-util-to-markdown | 2.1.2 | MIT |
| mdast-util-to-string | 4.0.0 | MIT |
| micromark | 4.0.2 | MIT |
| micromark-core-commonmark | 2.0.3 | MIT |
| micromark-extension-gfm | 3.0.0 | MIT |
| micromark-extension-gfm-autolink-literal | 2.1.0 | MIT |
| micromark-extension-gfm-footnote | 2.1.0 | MIT |
| micromark-extension-gfm-strikethrough | 2.1.0 | MIT |
| micromark-extension-gfm-table | 2.1.1 | MIT |
| micromark-extension-gfm-tagfilter | 2.0.0 | MIT |
| micromark-extension-gfm-task-list-item | 2.1.0 | MIT |
| micromark-factory-destination | 2.0.1 | MIT |
| micromark-factory-label | 2.0.1 | MIT |
| micromark-factory-space | 2.0.1 | MIT |
| micromark-factory-title | 2.0.1 | MIT |
| micromark-factory-whitespace | 2.0.1 | MIT |
| micromark-util-character | 2.1.1 | MIT |
| micromark-util-chunked | 2.0.1 | MIT |
| micromark-util-classify-character | 2.0.1 | MIT |
| micromark-util-combine-extensions | 2.0.1 | MIT |
| micromark-util-decode-numeric-character-reference | 2.0.2 | MIT |
| micromark-util-decode-string | 2.0.1 | MIT |
| micromark-util-encode | 2.0.1 | MIT |
| micromark-util-html-tag-name | 2.0.1 | MIT |
| micromark-util-normalize-identifier | 2.0.1 | MIT |
| micromark-util-resolve-all | 2.0.1 | MIT |
| micromark-util-sanitize-uri | 2.0.1 | MIT |
| micromark-util-subtokenize | 2.1.0 | MIT |
| micromark-util-symbol | 2.0.1 | MIT |
| micromark-util-types | 2.0.2 | MIT |
| ms | 2.1.3 | MIT |
| nanoid | 3.3.20 | MIT |
| negotiator | 1.0.0 | MIT |
| next | 15.5.29 | MIT |
| next-intl | 4.13.4 | MIT |
| next-intl-swc-plugin-extractor | 4.13.4 | MIT |
| node-addon-api | 7.1.1 | MIT |
| p-limit | 2.3.0 | MIT |
| p-locate | 4.1.0 | MIT |
| p-try | 2.2.0 | MIT |
| parse-entities | 4.0.2 | MIT |
| path-exists | 4.0.0 | MIT |
| picocolors | 1.1.1 | voir le paquet |
| picomatch | 4.0.7 | MIT |
| pngjs | 5.0.0 | MIT |
| po-parser | 2.1.1 | MIT |
| postcss | 8.5.23 | MIT |
| property-information | 7.2.0 | MIT |
| qrcode | 1.5.4 | MIT |
| react | 19.2.9 | MIT |
| react-dom | 19.2.9 | MIT |
| react-markdown | 10.1.0 | MIT |
| remark-gfm | 4.0.1 | MIT |
| remark-parse | 11.0.0 | MIT |
| remark-rehype | 11.1.2 | MIT |
| remark-stringify | 11.0.0 | MIT |
| require-directory | 2.1.1 | MIT |
| require-main-filename | 2.0.0 | ISC |
| scheduler | 0.27.0 | MIT |
| semver | 7.8.6 | ISC |
| set-blocking | 2.0.0 | ISC |
| sharp | 0.35.5 | Apache-2.0 |
| source-map-js | 1.2.2 | BSD-3-Clause |
| space-separated-tokens | 2.0.2 | MIT |
| string-width | 4.2.3 | MIT |
| stringify-entities | 4.0.4 | MIT |
| strip-ansi | 6.0.1 | MIT |
| style-to-js | 1.1.21 | MIT |
| style-to-object | 1.0.14 | MIT |
| styled-jsx | 5.1.6 | MIT |
| trim-lines | 3.0.1 | MIT |
| trough | 2.2.0 | MIT |
| tslib | 2.8.1 | voir le paquet |
| unified | 11.0.5 | MIT |
| unist-util-is | 6.0.1 | MIT |
| unist-util-position | 5.0.0 | MIT |
| unist-util-stringify-position | 4.0.0 | MIT |
| unist-util-visit | 5.1.0 | MIT |
| unist-util-visit-parents | 6.0.2 | MIT |
| use-intl | 4.13.4 | MIT |
| vfile | 6.0.3 | MIT |
| vfile-message | 4.0.3 | MIT |
| which-module | 2.0.1 | ISC |
| wrap-ansi | 6.2.0 | MIT |
| y18n | 4.0.3 | ISC |
| yargs | 15.4.1 | MIT |
| yargs-parser | 18.1.3 | ISC |
| zwitch | 2.0.4 | MIT |

## Paquets npm du frontend — dépendances de développement (non redistribuées dans l'image)

| Paquet | Version | Licence (package-lock) |
|---|---|---|
| @adobe/css-tools | 4.5.0 | MIT |
| @asamuzakjp/css-color | 3.2.0 | MIT |
| @babel/code-frame | 7.29.7 | MIT |
| @babel/compat-data | 7.29.7 | MIT |
| @babel/core | 7.29.7 | MIT |
| @babel/generator | 7.29.7 | MIT |
| @babel/helper-compilation-targets | 7.29.7 | MIT |
| @babel/helper-globals | 7.29.7 | MIT |
| @babel/helper-module-imports | 7.29.7 | MIT |
| @babel/helper-module-transforms | 7.29.7 | MIT |
| @babel/helper-plugin-utils | 7.29.7 | MIT |
| @babel/helper-string-parser | 7.29.7 | MIT |
| @babel/helper-validator-identifier | 7.29.7 | MIT |
| @babel/helper-validator-option | 7.29.7 | MIT |
| @babel/helpers | 7.29.7 | MIT |
| @babel/parser | 7.29.7 | MIT |
| @babel/plugin-transform-react-jsx-self | 7.29.7 | MIT |
| @babel/plugin-transform-react-jsx-source | 7.29.7 | MIT |
| @babel/runtime | 7.29.7 | MIT |
| @babel/template | 7.29.7 | MIT |
| @babel/traverse | 7.29.7 | MIT |
| @babel/types | 7.29.7 | MIT |
| @csstools/color-helpers | 5.1.0 | MIT-0 |
| @csstools/css-calc | 2.1.4 | MIT |
| @csstools/css-color-parser | 3.1.0 | MIT |
| @csstools/css-parser-algorithms | 3.0.5 | MIT |
| @csstools/css-tokenizer | 3.0.4 | MIT |
| @esbuild/aix-ppc64 | 0.28.2 | MIT |
| @esbuild/android-arm | 0.28.2 | MIT |
| @esbuild/android-arm64 | 0.28.2 | MIT |
| @esbuild/android-x64 | 0.28.2 | MIT |
| @esbuild/darwin-arm64 | 0.28.2 | MIT |
| @esbuild/darwin-x64 | 0.28.2 | MIT |
| @esbuild/freebsd-arm64 | 0.28.2 | MIT |
| @esbuild/freebsd-x64 | 0.28.2 | MIT |
| @esbuild/linux-arm | 0.28.2 | MIT |
| @esbuild/linux-arm64 | 0.28.2 | MIT |
| @esbuild/linux-ia32 | 0.28.2 | MIT |
| @esbuild/linux-loong64 | 0.28.2 | MIT |
| @esbuild/linux-mips64el | 0.28.2 | MIT |
| @esbuild/linux-ppc64 | 0.28.2 | MIT |
| @esbuild/linux-riscv64 | 0.28.2 | MIT |
| @esbuild/linux-s390x | 0.28.2 | MIT |
| @esbuild/linux-x64 | 0.28.2 | MIT |
| @esbuild/netbsd-arm64 | 0.28.2 | MIT |
| @esbuild/netbsd-x64 | 0.28.2 | MIT |
| @esbuild/openbsd-arm64 | 0.28.2 | MIT |
| @esbuild/openbsd-x64 | 0.28.2 | MIT |
| @esbuild/openharmony-arm64 | 0.28.2 | MIT |
| @esbuild/sunos-x64 | 0.28.2 | MIT |
| @esbuild/win32-arm64 | 0.28.2 | MIT |
| @esbuild/win32-ia32 | 0.28.2 | MIT |
| @esbuild/win32-x64 | 0.28.2 | MIT |
| @jridgewell/gen-mapping | 0.3.13 | MIT |
| @jridgewell/remapping | 2.3.5 | MIT |
| @jridgewell/resolve-uri | 3.1.2 | MIT |
| @jridgewell/sourcemap-codec | 1.5.5 | MIT |
| @jridgewell/trace-mapping | 0.3.31 | MIT |
| @rolldown/pluginutils | 1.0.0-beta.27 | MIT |
| @rollup/rollup-android-arm-eabi | 4.68.2 | MIT |
| @rollup/rollup-android-arm64 | 4.68.2 | MIT |
| @rollup/rollup-android-x64 | 4.68.2 | MIT |
| @rollup/rollup-darwin-arm64 | 4.68.2 | MIT |
| @rollup/rollup-darwin-x64 | 4.68.2 | MIT |
| @rollup/rollup-freebsd-arm64 | 4.68.2 | MIT |
| @rollup/rollup-freebsd-x64 | 4.68.2 | MIT |
| @rollup/rollup-linux-arm-gnueabihf | 4.68.2 | MIT |
| @rollup/rollup-linux-arm-musleabihf | 4.68.2 | MIT |
| @rollup/rollup-linux-arm64-gnu | 4.68.2 | MIT |
| @rollup/rollup-linux-arm64-musl | 4.68.2 | MIT |
| @rollup/rollup-linux-loong64-gnu | 4.68.2 | MIT |
| @rollup/rollup-linux-loong64-musl | 4.68.2 | MIT |
| @rollup/rollup-linux-ppc64-gnu | 4.68.2 | MIT |
| @rollup/rollup-linux-ppc64-musl | 4.68.2 | MIT |
| @rollup/rollup-linux-riscv64-gnu | 4.68.2 | MIT |
| @rollup/rollup-linux-riscv64-musl | 4.68.2 | MIT |
| @rollup/rollup-linux-s390x-gnu | 4.68.2 | MIT |
| @rollup/rollup-linux-x64-gnu | 4.68.2 | MIT |
| @rollup/rollup-linux-x64-musl | 4.68.2 | MIT |
| @rollup/rollup-openbsd-x64 | 4.68.2 | MIT |
| @rollup/rollup-openharmony-arm64 | 4.68.2 | MIT |
| @rollup/rollup-win32-arm64-msvc | 4.68.2 | MIT |
| @rollup/rollup-win32-ia32-msvc | 4.68.2 | MIT |
| @rollup/rollup-win32-x64-gnu | 4.68.2 | MIT |
| @rollup/rollup-win32-x64-msvc | 4.68.2 | MIT |
| @standard-schema/spec | 1.1.0 | MIT |
| @testing-library/dom | 10.4.1 | MIT |
| @testing-library/jest-dom | 6.9.1 | MIT |
| @testing-library/react | 16.3.2 | MIT |
| @testing-library/user-event | 14.6.1 | MIT |
| @types/aria-query | 5.0.4 | MIT |
| @types/babel__core | 7.20.5 | MIT |
| @types/babel__generator | 7.27.0 | MIT |
| @types/babel__template | 7.4.4 | MIT |
| @types/babel__traverse | 7.28.0 | MIT |
| @types/chai | 5.2.3 | MIT |
| @types/deep-eql | 4.0.2 | MIT |
| @types/node | 25.0.3 | voir le paquet |
| @types/qrcode | 1.5.5 | MIT |
| @types/react | 19.2.7 | voir le paquet |
| @vitejs/plugin-react | 4.7.0 | MIT |
| @vitest/expect | 4.1.12 | MIT |
| @vitest/mocker | 4.1.12 | MIT |
| @vitest/pretty-format | 4.1.12 | MIT |
| @vitest/runner | 4.1.12 | MIT |
| @vitest/snapshot | 4.1.12 | MIT |
| @vitest/spy | 4.1.12 | MIT |
| @vitest/utils | 4.1.12 | MIT |
| agent-base | 7.1.4 | MIT |
| ansi-styles | 5.2.0 | MIT |
| aria-query | 5.3.0 | Apache-2.0 |
| assertion-error | 2.0.1 | MIT |
| asynckit | 0.4.0 | MIT |
| baseline-browser-mapping | 2.11.35 | Apache-2.0 |
| browserslist | 4.29.5 | MIT |
| call-bind-apply-helpers | 1.0.2 | MIT |
| chai | 6.3.0 | MIT |
| combined-stream | 1.0.8 | MIT |
| convert-source-map | 2.0.0 | MIT |
| css.escape | 1.5.1 | MIT |
| cssstyle | 4.6.0 | MIT |
| csstype | 3.2.3 | voir le paquet |
| data-urls | 5.0.0 | MIT |
| decimal.js | 10.6.0 | MIT |
| delayed-stream | 1.0.0 | MIT |
| dom-accessibility-api | 0.5.16 | MIT |
| dom-accessibility-api | 0.6.3 | MIT |
| dunder-proto | 1.0.1 | MIT |
| electron-to-chromium | 1.5.478 | ISC |
| entities | 6.0.1 | BSD-2-Clause |
| es-define-property | 1.0.1 | MIT |
| es-errors | 1.3.0 | MIT |
| es-module-lexer | 2.3.2 | MIT |
| es-object-atoms | 1.1.2 | MIT |
| es-set-tostringtag | 2.1.0 | MIT |
| esbuild | 0.28.2 | MIT |
| escalade | 3.2.0 | MIT |
| estree-walker | 3.0.3 | MIT |
| expect-type | 1.3.0 | Apache-2.0 |
| fdir | 6.5.0 | MIT |
| form-data | 4.0.6 | MIT |
| fsevents | 2.3.3 | MIT |
| function-bind | 1.1.2 | MIT |
| gensync | 1.0.0-beta.2 | MIT |
| get-intrinsic | 1.3.0 | MIT |
| get-proto | 1.0.1 | MIT |
| gopd | 1.2.0 | MIT |
| has-symbols | 1.1.0 | MIT |
| has-tostringtag | 1.0.2 | MIT |
| hasown | 2.0.4 | MIT |
| html-encoding-sniffer | 4.0.0 | MIT |
| http-proxy-agent | 7.0.2 | MIT |
| https-proxy-agent | 7.0.6 | MIT |
| iconv-lite | 0.6.3 | MIT |
| indent-string | 4.0.0 | MIT |
| is-potential-custom-element-name | 1.0.1 | MIT |
| js-tokens | 4.0.0 | voir le paquet |
| jsdom | 25.0.1 | MIT |
| jsesc | 3.1.0 | MIT |
| json5 | 2.2.3 | MIT |
| lru-cache | 10.4.3 | ISC |
| lru-cache | 5.1.1 | ISC |
| lz-string | 1.5.0 | MIT |
| magic-string | 0.30.21 | MIT |
| math-intrinsics | 1.1.0 | MIT |
| mime-db | 1.52.0 | MIT |
| mime-types | 2.1.35 | MIT |
| min-indent | 1.0.1 | MIT |
| node-releases | 2.0.63 | MIT |
| nwsapi | 2.2.23 | MIT |
| obug | 2.2.1 | MIT |
| parse5 | 7.3.0 | MIT |
| pathe | 2.0.3 | MIT |
| pretty-format | 27.5.1 | MIT |
| punycode | 2.3.1 | MIT |
| react-is | 17.0.2 | MIT |
| react-refresh | 0.17.0 | MIT |
| redent | 3.0.0 | MIT |
| rollup | 4.68.2 | MIT |
| rrweb-cssom | 0.7.1 | MIT |
| rrweb-cssom | 0.8.0 | MIT |
| safer-buffer | 2.1.2 | MIT |
| saxes | 6.0.0 | ISC |
| semver | 6.3.1 | ISC |
| siginfo | 2.0.0 | ISC |
| stackback | 0.0.2 | MIT |
| std-env | 4.3.0 | MIT |
| strip-indent | 3.0.0 | MIT |
| symbol-tree | 3.2.4 | MIT |
| tinybench | 2.9.0 | MIT |
| tinyexec | 1.3.1 | MIT |
| tinyglobby | 0.2.17 | MIT |
| tinyrainbow | 3.2.0 | MIT |
| tldts | 6.1.86 | MIT |
| tldts-core | 6.1.86 | MIT |
| tough-cookie | 5.1.2 | BSD-3-Clause |
| tr46 | 5.1.1 | MIT |
| typescript | 5.9.3 | voir le paquet |
| undici-types | 7.16.0 | voir le paquet |
| update-browserslist-db | 1.3.4 | MIT |
| vite | 7.3.8 | MIT |
| vitest | 4.1.12 | MIT |
| w3c-xmlserializer | 5.0.0 | MIT |
| webidl-conversions | 7.0.0 | BSD-2-Clause |
| whatwg-encoding | 3.1.1 | MIT |
| whatwg-mimetype | 4.0.0 | MIT |
| whatwg-url | 14.2.0 | MIT |
| why-is-node-running | 2.3.0 | MIT |
| ws | 8.21.0 | MIT |
| xml-name-validator | 5.0.0 | Apache-2.0 |
| xmlchars | 2.2.0 | MIT |
| yallist | 3.1.1 | ISC |

## Kit Compact

Le kit Compact (`compact/`) ne redistribue aucun composant tiers : il référence par digest les images officielles et télécharge les modèles depuis leurs bibliothèques publiques lors d'une phase d'approvisionnement explicite (`compact/docs/approvisionnement-modeles.md`). Les licences des modèles sont rappelées dans `ai-gateway-open/THIRD_PARTY_NOTICES.md`.

Inventaires machine-readable : SBOM CycloneDX des images (produits à chaque release).
