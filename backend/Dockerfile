# backend/Dockerfile
#
# `WITH_DWG` est déclaré AVANT le premier `FROM` : un ARG utilisé dans une instruction
# `FROM` doit vivre dans la portée globale du Dockerfile.
ARG WITH_DWG=0

# image de base épinglée par digest (liste de manifestes multi-arch de `python:3.12-slim`,
# relevée le 2026-11-09). Une mise à jour de base est un changement explicite, jamais implicite.

# ---------------------------------------------------------------------------
# Convertisseur DWG optionnel — worker DÉDIÉ `worker_dwg`.
#
# GNU LibreDWG 0.14 est un CONVERTISSEUR (DWG -> DXF ASCII), jamais le parseur
# sémantique de RagATM : c'est le DxfExtractor qui lit le DXF produit. Licence
# GPLv3+ : il est utilisé comme EXÉCUTABLE SÉPARÉ (sous-processus), jamais lié à
# l'application, et la revue de conformité reste une porte de publication ouverte.
#
# Version ÉPINGLÉE et somme de contrôle VÉRIFIÉE : le writer DXF de 0.13.3 corrompt
# les MTEXT longs (documents entiers perdus) — la 0.14 est la première release qui
# le corrige (qualification interne). La construction échoue si
# l'archive téléchargée ne correspond pas au condensé attendu.
#
# Seul `dwg2dxf` est embarqué : ni `dwgread`, ni `dxf2dwg` (writer expérimental,
# jamais source de vérité), ni `dwg2SVG`.
# ---------------------------------------------------------------------------
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS libredwg-1
ARG LIBREDWG_VERSION=0.14
ARG LIBREDWG_SHA256=62ebb73b984f865960f20ed26619ea5f8789d5e3fd088fa40a2598384da81275
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl pkg-config xz-utils; \
    rm -rf /var/lib/apt/lists/*; \
    cd /tmp; \
    curl -fsSLo libredwg.tar.xz "https://ftp.gnu.org/gnu/libredwg/libredwg-${LIBREDWG_VERSION}.tar.xz"; \
    echo "${LIBREDWG_SHA256}  libredwg.tar.xz" | sha256sum -c -; \
    tar xf libredwg.tar.xz; \
    cd "libredwg-${LIBREDWG_VERSION}"; \
    ./configure --disable-bindings --disable-python --disable-shared --enable-static \
      --disable-dependency-tracking; \
    make -j"$(nproc)"; \
    mkdir -p /out/bin /out/share/doc/libredwg; \
    install -m 0755 programs/dwg2dxf /out/bin/dwg2dxf; \
    install -m 0644 COPYING /out/share/doc/libredwg/COPYING; \
    install -m 0644 NEWS /out/share/doc/libredwg/NEWS; \
    /out/bin/dwg2dxf --version | grep -Fx "dwg2dxf ${LIBREDWG_VERSION}"

# Variante SANS convertisseur : arborescence vide, sélectionnée quand WITH_DWG=0.
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS libredwg-0
RUN mkdir -p /out/bin /out/share

# Sélection par ARG (patron BuildKit) : `dwg-artifacts` = la variante retenue.
FROM libredwg-${WITH_DWG} AS dwg-artifacts

FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# deps système minimales pour psycopg[binary] + build wheels éventuelles
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

# Dépendances d'exécution VERROUILLÉES. `requirements.lock` est GÉNÉRÉ
# depuis `pyproject.toml` (+ `requirements-runtime.in`, extra [tables]) par
# `backend/lock-runtime.sh` (uv pip compile --generate-hashes) : versions ET empreintes
# de toutes les dépendances, directes et transitives. `--require-hashes` fait échouer la
# construction si un artefact téléchargé ne correspond pas — deux constructions depuis le
# même lock installent exactement les mêmes distributions. Ne pas éditer à la main.
# Extra [tables] (pdfplumber, pur-python) inclus par DÉFAUT : extraction tabulaire
# text-native active en prod. L'OCR (image/PDF) reste piloté par WITH_OCR (ci-dessous).
COPY pyproject.toml requirements.lock requirements-ocr.lock requirements-ifc.lock /app/
RUN pip install --no-cache-dir --require-hashes --no-deps -r requirements.lock

# -------------------------------------------------------------
# OCR optionnel (STAGING uniquement) — cf. docs/DOCUMENT_EXTRACTION.md
# Build sans OCR par défaut (prod). Pour staging :
#   docker build --build-arg WITH_OCR=1 ...
# Ajoute le binaire Tesseract (fra+eng) + l'extra Python [ocr]
# (pytesseract, Pillow, pypdfium2 — rasteriseur PDF autonome, pas de poppler).
# L'image reste utilisable sans OCR : si absent, l'extraction échoue proprement
# (ocr_engine_missing) sans casser le reste du pipeline.
# -------------------------------------------------------------
ARG WITH_OCR=0
RUN if [ "$WITH_OCR" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng \
      && rm -rf /var/lib/apt/lists/* \
      && pip install --no-cache-dir --require-hashes --no-deps -r requirements-ocr.lock ; \
    else \
      echo "OCR non inclus dans l'image (WITH_OCR=0)." ; \
    fi

# -------------------------------------------------------------
# IFC optionnel — worker DÉDIÉ `worker_ifc` uniquement.
# Build sans IFC par défaut : l'image générale (backend, worker, beat…) reste
# sans IfcOpenShell (~147 Mo de bibliothèque native, ~115 Mo RSS à l'import).
# Pour le worker de la file `ingest_ifc` :
#   docker build --build-arg WITH_IFC=1 ...
# Ajoute l'extra Python [ifc] (ifcopenshell==0.8.5, wheel autonome ; aucun paquet
# système, aucune pile graphique, aucun moteur géométrique). Même code, même
# révision : seule la dépendance diffère. Sans l'extra, un IFC est refusé
# proprement par l'extracteur (ifc_dependency_unavailable).
# -------------------------------------------------------------
ARG WITH_IFC=0
RUN if [ "$WITH_IFC" = "1" ]; then \
      pip install --no-cache-dir --require-hashes --no-deps -r requirements-ifc.lock ; \
    else \
      echo "IFC non inclus dans l'image (WITH_IFC=0)." ; \
    fi

# -------------------------------------------------------------
# DWG optionnel — worker DÉDIÉ `worker_dwg` uniquement.
# L'image générale (backend, worker, beat…) reste SANS convertisseur : un `.dwg`
# y est admis par le seul flag et échoue proprement côté extracteur
# (dwg_dependency_unavailable) si la tâche n'atterrit pas sur worker_dwg.
# Pour le worker de la file `ingest_dwg` :
#   docker build --build-arg WITH_DWG=1 ...
# Aucune dépendance Python ajoutée : le convertisseur est un exécutable séparé
# (GPLv3+, cf. /usr/local/share/doc/libredwg/COPYING dans l'image du worker :
# `/out/share/` de l'étape `dwg-artifacts` est copié sous `/usr/local/share/`).
# -------------------------------------------------------------
ARG WITH_DWG=0
COPY --from=dwg-artifacts /out/bin/ /usr/local/bin/
COPY --from=dwg-artifacts /out/share/ /usr/local/share/
RUN if [ "$WITH_DWG" = "1" ]; then \
      dwg2dxf --version ; \
    else \
      echo "DWG non inclus dans l'image (WITH_DWG=0)." ; \
    fi

# -------------------------------------------------------------
# AUDIO optionnel — worker DÉDIÉ `worker_audio` uniquement.
# ffmpeg/ffprobe (paquet Debian) servent à INSPECTER l'audio (décodable, piste audio,
# pas de vidéo, durée) et à le PRÉPARER (FLAC 16 kHz mono, découpe au-delà de 90 min)
# avant la transcription par AI Gateway. Aucun moteur ASR dans l'image : la
# reconnaissance reste sur le Gateway. L'image générale reste sans ffmpeg ; un audio
# y est admis au dépôt par le seul flag et sa chaîne ASR part sur `ingest_audio`.
#   docker build --build-arg WITH_AUDIO=1 ...
# -------------------------------------------------------------
ARG WITH_AUDIO=0
RUN if [ "$WITH_AUDIO" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends ffmpeg \
      && rm -rf /var/lib/apt/lists/* \
      && ffmpeg -hide_banner -version | head -1 && ffprobe -hide_banner -version | head -1 ; \
    else \
      echo "AUDIO non inclus dans l'image (WITH_AUDIO=0)." ; \
    fi

# Copier le code après install deps pour maximiser le cache Docker
COPY . /app
# Le paquet local lui-même : sans dépendances (toutes déjà installées depuis le lock) et
# sans isolation de construction (setuptools/wheel figés dans le lock, aucun téléchargement).
RUN pip install --no-cache-dir --no-deps --no-build-isolation /app \
    && rm -rf /app/build /app/*.egg-info

# Entrypoint (migrations/collectstatic optionnels + start gunicorn)
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# ---------------------------------------------------------------------------
# Révision construite — injectée par le BUILD, jamais saisie à la main.
# ---------------------------------------------------------------------------
# APP_GIT_SHA était posée dans `.env` par l'opérateur. En production elle a
# annoncé pendant plusieurs jours un commit vieux de 34 révisions : toute trace
# d'observabilité, tout rapport d'incident et tout diagnostic attribuaient les
# mesures au mauvais code. Une provenance qui se saisit à la main finit toujours
# par mentir — elle doit venir de ce qui est réellement construit.
#
# Trois surfaces, une seule source :
#   - `ARG APP_GIT_SHA` : passé par le compose (`--build-arg`) ;
#   - `ENV APP_GIT_SHA` : lu par l'application au démarrage ;
#   - LABEL OCI : lisible sur l'IMAGE, même conteneur arrêté (`docker inspect`).
ARG APP_GIT_SHA=""
ENV APP_GIT_SHA=${APP_GIT_SHA}
LABEL org.opencontainers.image.revision=$APP_GIT_SHA

# Trace IMMUABLE de la révision construite. Contrairement à `ENV`, ce fichier ne
# peut pas être écrasé par une variable du compose : c'est lui qui permet au
# system check `config.E011` de détecter qu'un `.env` annonce une révision que
# l'image ne contient pas — exactement le défaut constaté en production.
RUN printf '%s' "${APP_GIT_SHA}" > /app/BUILD_REVISION

# Exécution NON privilégiée : utilisateur `app` (UID/GID 10001). Le code est lu seul ; les seuls répertoires
# d'écriture sont le volume des documents (`/data/media`, MEDIA_ROOT en production), `/tmp` (planificateur Celery
# beat, fichiers temporaires d'extraction) et le répertoire des fichiers statiques collectés. Les journaux vont sur
# la sortie standard. Un volume `media` créé par une version antérieure (propriété root) doit être réattribué
# (`chown -R 10001:10001`) : voir compact/docs/installation.md.
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data/media /app/staticfiles \
    && chown -R app:app /app/staticfiles /data/media
USER app

ARG BACKEND_PORT=8010
EXPOSE ${BACKEND_PORT}

# gunicorn en WSGI
CMD ["/entrypoint.sh"]
