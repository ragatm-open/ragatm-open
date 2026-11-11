"""Manifeste d'un rafraîchissement production -> staging, et sa vérification.

POURQUOI
--------
Un rafraîchissement de staging manipule DEUX choses : les lignes de base et les
fichiers médias. Rien n'imposait jusqu'ici qu'elles arrivent ensemble. Le résultat
observé le 01/08/2026 sur l'espace AEC — **268 lignes importées, 222 fichiers
copiés** — n'a produit aucune erreur : la copie s'est terminée « normalement » et
l'écart n'est apparu que trois semaines plus tard.

Ce module traite base et médias comme **deux moitiés d'un même instantané
logique**. Il produit un manifeste avant copie (côté source) et après copie (côté
destination), puis refuse de conclure au succès si les deux ne se recouvrent pas.

FAIL CLOSED
-----------
:func:`verify` ne renvoie ``COMPLETE`` que si tout concorde. Le doute vaut échec :
un manifeste illisible, un domaine absent, un compte inférieur — tout produit
``INCOMPLETE``. Un rafraîchissement muet qui a perdu des fichiers est bien pire
qu'un rafraîchissement qui échoue bruyamment.

READ-ONLY
---------
Ce module ne copie rien, ne supprime rien, ne modifie aucune ligne. Il compte,
mesure et compare. La copie elle-même reste au script d'exploitation
(``scripts/staging/refresh_from_production.sh``), qui appelle ce module de part et
d'autre.

CONFIDENTIALITÉ
---------------
Le manifeste ne contient **aucun nom de fichier ni de document** : uniquement des
compteurs, des volumes et une empreinte agrégée. Il peut donc être archivé et
transporté sans précaution particulière.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

from django.conf import settings

#: Version du format. Toute modification incompatible doit l'incrémenter :
#: `verify` refuse de comparer deux manifestes de versions différentes.
MANIFEST_VERSION = 1

#: Domaines de stockage, alignés sur les ``upload_to`` des FileField (ADR-001).
#: Même liste que ``media_storage_audit.MANAGED_SUBDIRS`` — un domaine oublié ici
#: serait un domaine copié sans être vérifié.
DOMAINS = ("rag_docs", "corpus_docs")

RESULT_COMPLETE = "COMPLETE"
RESULT_INCOMPLETE = "INCOMPLETE"

#: Empreinte par (chemin, taille) — bon marché, détecte l'absence et la troncature.
DIGEST_SIZE = "path_size"
#: Empreinte par (chemin, sha256 du contenu) — détecte aussi la corruption.
DIGEST_CONTENT = "path_sha256"


def _models_for_domain(domain: str):
    from apps.corpus.models import DomainCorpusDocument
    from apps.rag_core.models import Document

    return {"rag_docs": Document, "corpus_docs": DomainCorpusDocument}[domain]


def _referenced(domain: str) -> list[str]:
    """Chemins ``storage_file`` non vides déclarés en base pour ce domaine."""
    model = _models_for_domain(domain)
    qs = model.objects.exclude(storage_file="").exclude(storage_file__isnull=True)
    # `.iterator()` : jamais la table entière en mémoire.
    return [n for n in qs.values_list("storage_file", flat=True).iterator() if n]


def _document_count(domain: str) -> int:
    return int(_models_for_domain(domain).objects.count())


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


#: Longueur des empreintes par fichier. 64 bits : les collisions sont
#: négligeables à l'échelle d'un parc documentaire, et le manifeste reste court.
FINGERPRINT_LEN = 16


def _fingerprint(name: str, marker: str) -> str:
    """Empreinte OPAQUE d'un fichier — ni son nom, ni son contenu.

    C'est elle qui permet une vérification par INCLUSION : la destination doit
    contenir toutes les empreintes de la source. Un simple total serait aveugle
    au cas « autant de fichiers, mais pas les mêmes », et une empreinte agrégée
    unique interdirait à la destination de porter des documents supplémentaires —
    or staging en ingère légitimement en local.
    """
    h = hashlib.sha256()
    h.update(name.encode("utf-8"))
    h.update(b"\0")
    h.update(marker.encode("ascii"))
    return h.hexdigest()[:FINGERPRINT_LEN]


def _digest(fingerprints: Iterable[str]) -> str:
    """Empreinte globale, dérivée des empreintes par fichier."""
    h = hashlib.sha256()
    for fp in sorted(fingerprints):
        h.update(fp.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def _files_on_disk(media_root: Path, domain: str) -> set[str]:
    base = media_root / domain
    if not base.is_dir():
        return set()
    return {
        str(p.relative_to(media_root))
        for p in base.rglob("*")
        if p.is_file()
    }


def collect(*, environment: str, role: str, digest_mode: str = DIGEST_SIZE) -> dict[str, Any]:
    """Instantané vérifiable d'un environnement. Ne modifie rien.

    ``digest_mode`` : :data:`DIGEST_SIZE` (défaut, ``stat`` seul) ou
    :data:`DIGEST_CONTENT` (lit chaque fichier — plus lent, détecte la corruption).
    Les deux côtés d'une vérification doivent employer le MÊME mode ; sinon les
    empreintes ne sont pas comparées et :func:`verify` le dit explicitement.
    """
    if digest_mode not in (DIGEST_SIZE, DIGEST_CONTENT):
        raise ValueError(f"digest_mode inconnu : {digest_mode}")

    media_root = Path(settings.MEDIA_ROOT)
    domains: dict[str, Any] = {}

    for domain in DOMAINS:
        referenced = _referenced(domain)
        on_disk = _files_on_disk(media_root, domain)

        fingerprints: list[str] = []
        missing = 0
        bytes_existing = 0
        for name in referenced:
            p = media_root / name
            try:
                if not p.is_file():
                    missing += 1
                    continue
                size = p.stat().st_size
            except OSError:
                # Un fichier illisible n'est pas un fichier présent : le compter
                # comme tel ferait passer une copie dégradée pour complète.
                missing += 1
                continue
            bytes_existing += size
            marker = _sha256_file(p) if digest_mode == DIGEST_CONTENT else str(size)
            fingerprints.append(_fingerprint(name, marker))

        referenced_set = set(referenced)
        domains[domain] = {
            "document_count": _document_count(domain),
            "referenced_media_count": len(referenced_set),
            "existing_media_count": len(fingerprints),
            "missing_media_count": missing,
            "bytes_existing": bytes_existing,
            "files_on_disk": len(on_disk),
            "orphan_file_count": len(on_disk - referenced_set),
            "digest": _digest(fingerprints),
            "fingerprints": sorted(set(fingerprints)),
        }

    totals = {
        key: sum(d[key] for d in domains.values())
        for key in (
            "document_count", "referenced_media_count", "existing_media_count",
            "missing_media_count", "bytes_existing", "files_on_disk", "orphan_file_count",
        )
    }

    return {
        "manifest_version": MANIFEST_VERSION,
        "environment": environment,
        "role": role,
        "media_root": str(media_root),
        "digest_mode": digest_mode,
        "domains": domains,
        "totals": totals,
    }


def _fail(reasons: list[str], code: str, detail: str) -> None:
    reasons.append(f"{code}: {detail}")


def verify(source: dict[str, Any], destination: dict[str, Any]) -> dict[str, Any]:
    """Compare deux manifestes. ``COMPLETE`` uniquement si tout concorde.

    Règles, toutes en échec fermé :

    1. la destination ne doit avoir **aucune** référence sans fichier ;
    2. elle doit détenir **au moins autant** de fichiers existants que la source
       en avait — c'est la règle qui aurait arrêté le cas « 268 lignes / 222
       fichiers » ;
    3. elle doit détenir **au moins autant** de documents que la source ;
    4. si les deux côtés emploient le même mode d'empreinte, **chaque empreinte
       de la source doit se retrouver en destination** — vérification par
       INCLUSION. Un total identique ne prouverait rien sur « les mêmes
       fichiers » ; une empreinte globale identique, elle, interdirait tout ajout.

    « Au moins » et par inclusion, jamais « exactement » : une destination peut
    légitimement porter des documents locaux supplémentaires (cas observé,
    20 documents ingérés en staging après l'import). Un EXCÉDENT n'est pas une
    perte ; un DÉFICIT l'est.
    """
    reasons: list[str] = []

    for label, m in (("source", source), ("destination", destination)):
        if not isinstance(m, dict):
            _fail(reasons, "manifest_unreadable", f"{label} n'est pas un manifeste")
            return {"result": RESULT_INCOMPLETE, "reasons": reasons, "domains": {}}
        if m.get("manifest_version") != MANIFEST_VERSION:
            _fail(
                reasons, "manifest_version_mismatch",
                f"{label} version={m.get('manifest_version')} attendu={MANIFEST_VERSION}",
            )

    if reasons:
        return {"result": RESULT_INCOMPLETE, "reasons": reasons, "domains": {}}

    compare_digests = source.get("digest_mode") == destination.get("digest_mode")
    if not compare_digests:
        reasons.append(
            "digest_mode_mismatch: empreintes non comparées "
            f"(source={source.get('digest_mode')} destination={destination.get('digest_mode')})"
        )

    per_domain: dict[str, Any] = {}
    for domain in DOMAINS:
        src = source.get("domains", {}).get(domain)
        dst = destination.get("domains", {}).get(domain)
        if src is None or dst is None:
            _fail(reasons, "domain_absent", f"{domain} absent d'un des manifestes")
            continue

        d_missing = int(dst["missing_media_count"])
        s_existing = int(src["existing_media_count"])
        d_existing = int(dst["existing_media_count"])
        s_docs = int(src["document_count"])
        d_docs = int(dst["document_count"])

        if d_missing:
            _fail(
                reasons, "destination_missing_sources",
                f"{domain}: {d_missing} référence(s) sans fichier après copie",
            )
        if d_existing < s_existing:
            _fail(
                reasons, "media_shortfall",
                f"{domain}: {d_existing} fichier(s) copié(s) pour {s_existing} attendu(s) "
                f"— déficit de {s_existing - d_existing}",
            )
        if d_docs < s_docs:
            _fail(
                reasons, "document_shortfall",
                f"{domain}: {d_docs} document(s) en destination pour {s_docs} en source",
            )
        not_transported = None
        if compare_digests:
            src_fp = set(src.get("fingerprints") or [])
            dst_fp = set(dst.get("fingerprints") or [])
            not_transported = len(src_fp - dst_fp)
            if not_transported:
                # Absent OU altéré : dans les deux cas le fichier attendu n'est
                # pas arrivé tel quel. Distinguer les deux exigerait de nommer les
                # fichiers dans le manifeste — ce qu'il s'interdit.
                _fail(
                    reasons, "fingerprint_shortfall",
                    f"{domain}: {not_transported} fichier(s) de la source absent(s) "
                    "ou altéré(s) en destination",
                )

        per_domain[domain] = {
            "source_documents": s_docs,
            "destination_documents": d_docs,
            "source_media_existing": s_existing,
            "destination_media_existing": d_existing,
            "destination_media_missing": d_missing,
            "source_files_not_transported": not_transported,
        }

    return {
        "result": RESULT_INCOMPLETE if reasons else RESULT_COMPLETE,
        "reasons": reasons,
        "domains": per_domain,
    }


def dumps(manifest: dict[str, Any]) -> str:
    return json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False)
