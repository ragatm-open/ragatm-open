"""Import d'un paquet d'incident en environnement de DÉVELOPPEMENT.

Cette commande est le point d'entrée du contenu de production dans un autre
environnement. Elle vérifie donc TOUT avant d'écrire quoi que ce soit :
signature, empreintes par fichier, version de format, expiration, environnement
d'origine, modèle d'embedding.

Le contrôle le plus important n'est pas cryptographique — c'est le modèle
d'embedding. Rejouer un incident produit sur ``text-embedding-3-large`` avec des
embeddings ``ollama`` donne des scores qui n'ont AUCUN rapport avec ceux de
l'incident : le replay conclurait, et il conclurait faux. Dès que le modèle
local est CONNU et divergent, l'import **refuse** — il n'avertit pas.

Le modèle local est celui du ``--rag-space`` de replay, car c'est le RagSpace
qui porte l'embedding effectif dans cette plateforme, pas un réglage global. Sans
espace désigné, la comparaison est déclarée impossible et signalée : refuser sur
une comparaison qu'on ne sait pas faire produirait des faux positifs, et un
contrôle qui se déclenche à tort finit désactivé.

Le paquet en clair est supprimé après import (``--delete-plaintext-after-import``) :
une archive déchiffrée qui traîne sur un poste de développement est exactement
le risque que tout le reste du dispositif cherche à éviter.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.feedback.export import crypto, package


class Command(BaseCommand):
    help = "Importe un paquet d'incident (développement uniquement)."

    def add_arguments(self, parser):
        parser.add_argument("--input", required=True, help="Chemin de l'archive (.zip ou .zip.enc).")
        parser.add_argument(
            "--workspace", default="DEV_REPLAY",
            help="Nom du workspace de replay (créé si absent).",
        )
        parser.add_argument(
            "--delete-plaintext-after-import", action="store_true",
            help="Supprime l'archive source après import. Recommandé.",
        )
        parser.add_argument(
            "--skip-embedding-check", action="store_true",
            help=(
                "Passe outre la divergence de modèle d'embedding. À N'UTILISER QUE "
                "pour un replay `prompt-only`, où les scores ne jouent aucun rôle."
            ),
        )
        parser.add_argument("--out", default="", help="Répertoire où déposer l'incident extrait.")
        parser.add_argument(
            "--rag-space", default="",
            help=(
                "UUID du RagSpace de replay. C'est LUI qui porte le modèle "
                "d'embedding effectif : sans lui, la comparaison des modèles ne "
                "peut pas être faite (avertissement au lieu d'un refus)."
            ),
        )

    def handle(self, *args, **opts):
        if not bool(getattr(settings, "FEEDBACK_INCIDENT_IMPORT_ENABLED", False)):
            raise CommandError(
                "Import désactivé (FEEDBACK_INCIDENT_IMPORT_ENABLED=0). Ce garde-fou "
                "existe pour qu'un paquet de production ne puisse pas être importé "
                "dans un environnement qui n'est pas prévu pour."
            )

        src = Path(opts["input"])
        if not src.is_file():
            raise CommandError(f"Archive introuvable : {src}")

        try:
            files = package.read_package(src.read_bytes())
        except crypto.EncryptionUnavailable as exc:
            raise CommandError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001
            raise CommandError(f"Archive illisible (error_code={type(exc).__name__}).") from None

        self._rag_space_id = opts["rag_space"]
        manifest = self._verify(files, skip_embedding_check=opts["skip_embedding_check"])

        out_dir = Path(opts["out"] or (src.parent / f"incident-{manifest['incident_id']}"))
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, blob in files.items():
            target = out_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)

        self.stdout.write(self.style.SUCCESS(f"Incident importé : {out_dir}"))
        self.stdout.write(f"  incident_id     : {manifest['incident_id']}")
        self.stdout.write(f"  origine         : {manifest['source_environment']} (git {manifest['git_sha'][:12] or '?'})")
        self.stdout.write(f"  capture         : {manifest['capture_mode']} (exacte={manifest['capture_is_exact']})")
        self.stdout.write(f"  embedding       : {manifest['embedding_model'] or 'non renseigné'}")
        self.stdout.write(f"  replay possible : {', '.join(manifest['replay_modes_supported']) or 'aucun'}")

        if opts["delete_plaintext_after_import"]:
            src.unlink()
            self.stdout.write(self.style.WARNING(f"  archive source supprimée : {src}"))
        else:
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠ Archive source CONSERVÉE. Utilisez "
                    "--delete-plaintext-after-import : une copie de contenu de "
                    "production sur un poste de développement doit être éphémère."
                )
            )

        # Le workspace de replay n'est créé qu'à la demande du mode `retrieval`
        # (cf. replay_rag_incident) : importer ne doit pas fabriquer d'espace
        # dont on n'a pas encore établi qu'il servira.
        return None

    # ------------------------------------------------------------------
    def _verify(self, files: dict[str, bytes], *, skip_embedding_check: bool) -> dict:
        if "manifest.json" not in files:
            raise CommandError("Paquet invalide : manifest.json absent.")

        try:
            manifest = json.loads(files["manifest.json"])
        except Exception:  # noqa: BLE001
            raise CommandError("manifest.json illisible.") from None

        # 1) Version de FORMAT — une majeure inconnue se refuse.
        version = int(manifest.get("schema_version") or 0)
        if version != package.PACKAGE_SCHEMA_VERSION:
            raise CommandError(
                f"Version de paquet non supportée : {version} "
                f"(attendue : {package.PACKAGE_SCHEMA_VERSION})."
            )

        # 2) Intégrité par fichier — localise une altération.
        expected_digests = manifest.get("file_sha256") or {}
        actual = crypto.file_digests(files)
        altered = [
            name for name, digest in expected_digests.items() if actual.get(name) != digest
        ]
        missing = [name for name in expected_digests if name not in actual]
        if altered or missing:
            raise CommandError(
                "Intégrité rompue : "
                + (f"fichiers modifiés {altered} " if altered else "")
                + (f"fichiers absents {missing}" if missing else "")
            )

        # 3) Empreinte globale + signature.
        recomputed = crypto.canonical_content_sha256(files)
        if recomputed != manifest.get("content_sha256"):
            raise CommandError("Intégrité rompue : content_sha256 ne correspond pas au contenu.")

        signature = manifest.get("signature") or ""
        if signature:
            if not crypto.verify_signature(recomputed, signature):
                raise CommandError(
                    "Signature INVALIDE : ce paquet ne provient pas de la chaîne "
                    "d'export attendue, ou la clé locale n'est pas la bonne."
                )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠ Paquet NON signé : son origine ne peut pas être établie."
                )
            )

        # 4) Expiration.
        expires_at = manifest.get("expires_at")
        if expires_at:
            try:
                deadline = datetime.fromisoformat(expires_at)
            except ValueError:
                deadline = None
            if deadline and deadline < timezone.now():
                raise CommandError(
                    f"Paquet EXPIRÉ le {expires_at}. Un contenu de production ne se "
                    "réimporte pas indéfiniment : demandez un nouvel export."
                )

        # 5) Environnement d'origine.
        current = str(getattr(settings, "FEEDBACK_INCIDENT_SOURCE_ENVIRONMENT", "dev"))
        if manifest.get("source_environment") == current:
            raise CommandError(
                f"Le paquet provient de l'environnement courant ({current}) : "
                "un import dans son propre environnement n'a pas de sens et "
                "risquerait d'écraser des données réelles."
            )

        # 6) Modèle d'embedding — le contrôle qui évite une conclusion fausse.
        package_model = str(manifest.get("embedding_model") or "")
        local_model = self._local_embedding_model(self._rag_space_id)
        if package_model and not local_model:
            # Refuser ici serait un FAUX POSITIF : sans espace de replay désigné,
            # le modèle local n'est pas déterminable (il est porté par le
            # RagSpace, pas par un réglage global). Un contrôle qui se déclenche
            # à tort finit désactivé — et ne protège alors plus rien.
            self.stdout.write(
                self.style.WARNING(
                    "  ⚠ Comparaison des modèles d'embedding IMPOSSIBLE "
                    f"(paquet={package_model!r}). Passez --rag-space <uuid> pour "
                    "que la vérification porte sur l'espace de replay réel."
                )
            )
        elif package_model and local_model and package_model != local_model:
            message = (
                f"Modèle d'embedding DIVERGENT : paquet={package_model!r}, "
                f"local={local_model!r}. Les scores ne seraient pas comparables et "
                "le replay conclurait sur des valeurs sans rapport avec l'incident."
            )
            if not skip_embedding_check:
                raise CommandError(message + " (--skip-embedding-check pour un replay prompt-only)")
            self.stdout.write(self.style.WARNING("  ⚠ " + message))

        if not manifest.get("capture_is_exact", True):
            self.stdout.write(
                self.style.WARNING(
                    f"  ⚠ Capture `{manifest.get('capture_mode')}` : ce n'est PAS l'état "
                    "d'origine de l'incident. Les écarts observés peuvent venir du "
                    "replay, pas du défaut."
                )
            )

        return manifest

    #: Renseigné par `handle` avant la vérification.
    _rag_space_id: str = ""

    @staticmethod
    def _local_embedding_model(rag_space_id: str = "") -> str:
        """Modèle d'embedding LOCAL, par ordre d'autorité décroissante.

        1. celui du RagSpace de replay — la seule valeur qui décrit vraiment
           l'index contre lequel le retrieval sera rejoué ;
        2. le réglage global, s'il porte un vrai nom de modèle. ``"default"`` est
           une sentinelle, pas un modèle : la comparer reviendrait à refuser tous
           les paquets ;
        3. rien — la vérification est alors déclarée impossible.
        """
        if rag_space_id:
            try:
                from apps.rag_core.models import RagSpace

                space = RagSpace.objects.filter(id=rag_space_id).first()
                if space is not None and getattr(space, "embedding_model", ""):
                    return str(space.embedding_model)
            except Exception:  # noqa: BLE001
                pass

        for name in ("EMBEDDINGS_MODEL", "RAG_EMBEDDING_MODEL", "OLLAMA_EMBEDDING_MODEL"):
            value = str(getattr(settings, name, "") or "").strip()
            if value and value.lower() not in ("default", "auto", "none"):
                return value
        return ""
