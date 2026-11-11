# backend/apps/rag_core/services/ingest/docvision/client.py
from __future__ import annotations

"""
Client du sidecar Document Vision (serveur de parsing MinerU LOCAL).

RAGatm ne dépend PAS du paquet `mineru` : le worker ne parle au moteur que par HTTP,
sur une boucle locale. Aucune dépendance Python n'est partagée entre les deux, ce qui
était l'exigence d'isolation la plus forte de la conception.

Trois refus par construction :

* **remote** — l'URL doit être une boucle locale ; toute autre cible échoue fermée,
  sans tentative de connexion. `mineru.net` n'est jamais joignable depuis ce client.
* **télémétrie** — l'état de consentement du sidecar est vérifié AVANT le premier
  document, et `unset` ne suffit pas : le service MinerU ne bloque l'envoi que sur
  `disabled`, l'état `unset` laisse passer pendant la préversion.
* **format** — seul `middle_json` est demandé, parce que c'est le seul qui porte la
  géométrie ; `html`, `latex` et `docx` exigeraient une clé d'API.
"""

import base64
import ipaddress
import json
import os
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from . import config

#: Hôtes acceptés pour le sidecar. Tout le reste est refusé sans connexion.
_HOTES_LOCAUX = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


class SidecarError(RuntimeError):
    """Échec du sidecar. `code` sert à choisir le repli, jamais à masquer l'échec."""

    def __init__(self, message: str, *, code: str = "mineru_error"):
        super().__init__(message)
        self.code = code


class SidecarRemoteRefused(SidecarError):
    """Une cible non locale a été demandée : on échoue fermé (§7)."""


class SidecarTelemetryRefused(SidecarError):
    """La télémétrie n'est pas prouvée désactivée : on échoue fermé (§6)."""


class SidecarTimeout(SidecarError):
    """Budget dépassé côté sidecar."""


class SidecarUnavailable(SidecarError):
    """Sidecar absent ou injoignable : le rail doit se replier, pas bloquer (§46)."""


def _adresses_privees(hote: str) -> bool:
    """Toutes les adresses résolues sont-elles privées ?

    Deux refus distincts importent ici : un nom non déclaré, et un nom déclaré qui
    résout vers l'extérieur. Le second est le seul qui pourrait faire sortir un
    document du poste, et c'est celui qu'une allowlist par nom seul laisserait passer.
    """
    try:
        infos = socket.getaddrinfo(hote, None)
    except OSError:
        return False
    adresses = {i[4][0] for i in infos}
    if not adresses:
        return False
    for a in adresses:
        try:
            ip = ipaddress.ip_address(a)
        except ValueError:
            return False
        if not (ip.is_private or ip.is_loopback or ip.is_link_local):
            return False
    return True


def _verifier_local(url: str) -> None:
    hote = (urllib.parse.urlparse(url).hostname or "").lower()
    if hote in _HOTES_LOCAUX:
        return
    if hote and hote in config.sidecar_hotes_autorises():
        if _adresses_privees(hote):
            return
        raise SidecarRemoteRefused(
            f"Sidecar Document Vision déclaré mais non privé : {hote!r} résout hors "
            "des plages privées. Refusé sans connexion.",
            code="mineru_remote_refused",
        )
    raise SidecarRemoteRefused(
        f"Sidecar Document Vision non local refusé : {hote!r}. Le rail n'autorise "
        "que la boucle locale ou un hôte privé explicitement déclaré ; "
        "aucun service distant.",
        code="mineru_remote_refused",
    )


class MineruSidecar:
    """Accès minimal au serveur de parsing local : santé, télémétrie, parsing."""

    def __init__(self, url: Optional[str] = None, *, timeout_s: Optional[int] = None):
        self.url = (url or config.sidecar_url()).rstrip("/")
        _verifier_local(self.url)
        self.timeout_s = timeout_s or config.page_timeout_s()

    # ------------------------------------------------------------------ transport
    def _appel(self, chemin: str, *, corps: Optional[dict] = None,
               brut: bool = False, timeout: Optional[int] = None) -> Any:
        url = self.url + chemin
        data = json.dumps(corps).encode("utf-8") if corps is not None else None
        req = urllib.request.Request(
            url, data=data, method="POST" if corps is not None else "GET",
            headers={"Content-Type": "application/json"} if corps is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as rep:
                return rep.read() if brut else json.load(rep)
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                pass
            raise SidecarError(f"Sidecar HTTP {e.code} sur {chemin} : {detail}",
                               code="mineru_http_error") from e
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            raise SidecarUnavailable(
                f"Sidecar Document Vision injoignable sur {self.url} : {e}",
                code="mineru_unavailable",
            ) from e

    # -------------------------------------------------------------------- contrôles
    def sante(self) -> dict:
        return self._appel("/health", timeout=10)

    def formats_disponibles(self) -> list:
        s = self.sante()
        return list(((s.get("features") or {}).get("output_formats")) or [])

    def verifier_demarrage(self) -> dict:
        """Contrôle de démarrage OBLIGATOIRE avant le premier document.

        L'aptitude se lit sur `/ready`, pas sur `/health`. Le serveur de MinerU ne
        publie rien sur sa télémétrie ni sur ses modèles : exiger cette preuve de
        `/health` revenait à demander à un composant une information qu'il ne
        produit pas, et le rail ne démarrait jamais. C'est l'enveloppe du sidecar
        qui atteste l'état, et elle échoue fermé.
        """
        d = self._appel("/ready", timeout=15)
        if not isinstance(d, dict) or not d.get("ready"):
            controles = (d or {}).get("checks") or {}
            manquants = sorted(k for k, v in controles.items() if not v)
            if "telemetry_attested" in manquants or "telemetry_component_absent" in manquants:
                raise SidecarTelemetryRefused(
                    "Télémétrie MinerU non prouvée désactivée par le sidecar "
                    f"(contrôles en échec : {manquants}). Aucun document ne sera traité.",
                    code="mineru_telemetry_not_disabled",
                )
            raise SidecarError(
                f"Sidecar non apte (contrôles en échec : {manquants or 'inconnus'}).",
                code="mineru_not_ready",
            )
        if not (d.get("checks") or {}).get("middle_json"):
            raise SidecarError(
                f"Le sidecar n'expose pas {config.output_format()!r}.",
                code="mineru_format_unavailable",
            )
        return {"telemetry": "disabled", "version": d.get("engine_version"),
                "tier": d.get("tier"), "device": d.get("device"),
                "attestation": d.get("attestation")}

    # ---------------------------------------------------------------------- parsing
    def parse_middle_json(self, nom: str, *, data: Optional[bytes] = None,
                          page_range: Optional[str] = None,
                          timeout_s: Optional[int] = None) -> Dict[str, Any]:
        """Parse un document transmis EN LIGNE et rend son `middle_json`.

        Le document voyage dans la requête plutôt que par un volume partagé. La
        source `local` obligerait à monter un répertoire documentaire dans le
        conteneur du moteur, avec le cycle de vie et la confidentialité qui vont
        avec ; elle est d'ailleurs refusée par défaut côté serveur. Mesure à
        l'appui, le corpus réel ne l'exige pas : sur 3443 documents, le plus gros
        pèse 24,4 Mo et p99 vaut 10,6 Mo, tous sous le plafond en ligne de 32 Mio.

        `page_range` n'est pas transmis pour les fichiers non-PDF : le serveur le
        refuse, et une image n'a de toute façon qu'une page.
        """
        budget = timeout_s or self.timeout_s
        t0 = time.monotonic()
        if data is None:
            raise SidecarError("Aucun contenu à transmettre au sidecar.",
                               code="mineru_no_payload")
        if len(data) > config.max_inline_bytes():
            raise SidecarError(
                f"Document de {len(data)} octets au-delà du plafond en ligne "
                f"({config.max_inline_bytes()}).", code="mineru_payload_too_large")
        entree: Dict[str, Any] = {"source": {
            "type": "inline", "name": os.path.basename(nom) or "document",
            "data": base64.b64encode(data).decode("ascii")}}
        if page_range and nom.lower().endswith(".pdf"):
            entree["page_range"] = page_range
        job = self._appel("/parse/jobs", corps={
            "files": [entree], "tier": config.tier(),
            "output_formats": [config.output_format()],
            # Budget transmis à l'enveloppe : au dépassement elle ANNULE réellement
            # le calcul. Sans lui, abandonner côté RAGatm laisserait le moteur
            # occuper son unique exécutant jusqu'au terme naturel du parse.
            "timeout_s": int(budget),
        }, timeout=budget + 15)
        jid = job.get("job_id")
        if not jid:
            raise SidecarError(f"Réponse de création de tâche inattendue : {job}",
                               code="mineru_bad_response")
        while True:
            reste = budget - (time.monotonic() - t0)
            if reste <= 0:
                raise SidecarTimeout(
                    f"Budget MinerU dépassé ({budget} s) sur {nom!r}.",
                    code="mineru_timeout")
            etat = self._appel(f"/parse/jobs/{jid}", timeout=max(1, int(reste)))
            if etat.get("status") in ("completed", "partial", "failed", "canceled"):
                break
            time.sleep(1.0)
        if etat.get("status") not in ("completed", "partial"):
            raise SidecarError(f"Parsing terminé en {etat.get('status')}.",
                               code="mineru_parse_failed")
        fichiers = etat.get("files") or []
        if not fichiers:
            raise SidecarError("Aucun fichier dans le résultat.", code="mineru_empty")
        sorties = (fichiers[0].get("output_files") or {})
        ref = sorties.get(config.output_format())
        if not ref or not ref.get("file_id"):
            raise SidecarError(f"Pas d'artefact {config.output_format()}.",
                               code="mineru_no_artifact")
        brut = self._appel(f"/files/{ref['file_id']}/content", brut=True,
                           timeout=max(1, int(budget - (time.monotonic() - t0))))
        try:
            mid = json.loads(brut.decode("utf-8"))
        except Exception as e:
            raise SidecarError(f"Artefact illisible : {e}", code="mineru_invalid_output")
        return {
            "middle_json": mid,
            "duree_s": round(time.monotonic() - t0, 2),
            "parser_version": (fichiers[0].get("parse") or {}).get("parser_version"),
            "artefact_id": ref["file_id"],
            "artefact_octets": ref.get("bytes"),
        }
