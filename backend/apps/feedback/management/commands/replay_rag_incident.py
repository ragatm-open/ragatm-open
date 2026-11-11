"""Replay d'un incident importé — trois modes, trois questions différentes.

    prompt-only    « le modèle se comporte-t-il correctement avec CE contexte ? »
    retrieval      « le bon chunk est-il candidat, et est-il retenu ? »
    full-pipeline  « le défaut se reproduit-il de bout en bout ? »

Choisir le mode, c'est choisir ce qu'on cherche. Le mode ``retrieval`` est le
seul DÉTERMINISTE, et c'est lui qui répond à la question des cas observés
(« I don't know » alors que le terme est indexé) : il ne fait aucun appel LLM,
donc son verdict est reproductible et peut devenir une assertion de CI.

``full-pipeline`` appelle un modèle de langage : son rapport est INFORMATIF. En
faire une assertion stricte produirait des tests qui échouent sans qu'aucun code
n'ait changé.
"""
from __future__ import annotations

import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

MODE_PROMPT_ONLY = "prompt-only"
MODE_RETRIEVAL = "retrieval"
MODE_FULL = "full-pipeline"


class Command(BaseCommand):
    help = "Rejoue un incident importé (développement uniquement)."

    def add_arguments(self, parser):
        parser.add_argument("--incident", required=True, help="Répertoire de l'incident importé.")
        parser.add_argument(
            "--mode", default=MODE_RETRIEVAL,
            choices=[MODE_PROMPT_ONLY, MODE_RETRIEVAL, MODE_FULL],
        )
        parser.add_argument(
            "--rag-space", default="",
            help="UUID d'un RagSpace de replay contenant les chunks (modes retrieval/full).",
        )
        parser.add_argument("--out", default="", help="Fichier JSON du rapport.")

    def handle(self, *args, **opts):
        if not bool(getattr(settings, "FEEDBACK_INCIDENT_IMPORT_ENABLED", False)):
            raise CommandError("Replay désactivé (FEEDBACK_INCIDENT_IMPORT_ENABLED=0).")

        root = Path(opts["incident"])
        if not root.is_dir():
            raise CommandError(f"Répertoire d'incident introuvable : {root}")

        files = self._load(root)
        manifest = files["manifest"]
        mode = opts["mode"]

        supported = manifest.get("replay_modes_supported") or []
        if supported and mode not in supported and mode != MODE_PROMPT_ONLY:
            self.stdout.write(
                self.style.WARNING(
                    f"  ⚠ Le paquet n'annonce pas le mode `{mode}` "
                    f"(annoncés : {', '.join(supported) or 'aucun'}). "
                    "Le rapport sera probablement incomplet."
                )
            )

        if mode == MODE_PROMPT_ONLY:
            report = self._replay_prompt_only(files)
        else:
            report = self._replay_retrieval(files, rag_space_id=opts["rag_space"], mode=mode)

        report["incident_id"] = manifest.get("incident_id")
        report["mode"] = mode
        report["source_capture_mode"] = manifest.get("capture_mode")
        report["source_capture_is_exact"] = manifest.get("capture_is_exact")

        payload = json.dumps(report, ensure_ascii=False, indent=2, default=str)
        if opts["out"]:
            Path(opts["out"]).write_text(payload, encoding="utf-8")
            self.stdout.write(self.style.SUCCESS(f"Rapport écrit : {opts['out']}"))
        self._render(report)
        return None

    # ------------------------------------------------------------------
    def _load(self, root: Path) -> dict:
        def _json(name: str) -> dict:
            path = root / name
            if not path.is_file():
                return {}
            return json.loads(path.read_text(encoding="utf-8"))

        manifest = _json("manifest.json")
        if not manifest:
            raise CommandError("manifest.json absent : ce répertoire n'est pas un incident importé.")
        return {
            "manifest": manifest,
            "request": _json("request.json"),
            "config": _json("effective_config.json"),
            "candidates": _json("retrieval_candidates.json"),
            "selected": _json("selected_chunks.json"),
            "generation": _json("generation.json"),
        }

    # ------------------------------------------------------------------
    def _replay_prompt_only(self, files: dict) -> dict:
        """Aucune base métier n'est touchée : seul le prompt capturé est rejoué."""
        generation = files["generation"]
        prompt = generation.get("prompt")
        if not prompt:
            raise CommandError(
                "Le paquet ne contient pas le prompt complet "
                f"(prompt_capture={generation.get('prompt_capture')!r}). "
                "Le mode prompt-only exige RAG_FEEDBACK_DIAGNOSTIC_CAPTURE_PROMPT=1 "
                "à la capture, et un export de niveau ≥ 2."
            )

        from apps.llm_router.router import generate_text

        provider = generation.get("provider") or None
        model = generation.get("model") or None
        # Aucun contexte d'egress n'est fourni : un appel sortant sans périmètre
        # est REFUSÉ par le routeur (fail-closed). Le replay `prompt-only` vise
        # donc un provider local ou la Gateway, jamais un tiers en aveugle.
        from apps.llm_router.egress import LlmExecutionContext

        # un prompt capturé peut contenir des extraits de
        # courriels ; le rejeu est STRICTEMENT local, quel que soit le provider capturé.
        response = generate_text(
            prompt=prompt, provider=provider, model=model, operation="feedback_replay",
            context=LlmExecutionContext.system_local_only(operation="feedback_replay"),
        )
        answer = (response or {}).get("text") or ""

        return {
            "production": {"answer_kind": generation.get("policy"), "answer": generation.get("answer")},
            "dev": {
                "answer_kind": None,
                "answer": answer,
                "provider": (response or {}).get("provider"),
                "model": (response or {}).get("model"),
            },
            "differences": {
                "answer_changed": (answer or "").strip() != (generation.get("answer") or "").strip()
            },
            "deterministic": False,
        }

    # ------------------------------------------------------------------
    def _replay_retrieval(self, files: dict, *, rag_space_id: str, mode: str) -> dict:
        if not rag_space_id:
            raise CommandError(
                "--rag-space est requis : les modes retrieval et full-pipeline "
                "interrogent PostgreSQL (FTS + pgvector). Aucun replay hors base "
                "n'est possible pour ces modes."
            )

        from apps.rag_core.models import RagSpace
        from apps.rag_core.services.query.orchestrator import query_rag

        space = RagSpace.objects.filter(id=rag_space_id).first()
        if space is None:
            raise CommandError(f"RagSpace de replay introuvable : {rag_space_id}")

        question = (files["request"] or {}).get("question")
        if not question:
            raise CommandError(
                "La question n'est pas présente dans le paquet (export de niveau 1, "
                "ou minimisation active à la capture) : le retrieval ne peut pas être rejoué."
            )

        captured: dict = {}
        result = query_rag(
            rag_space_id=space.id,
            question=question,
            user=None,  # aucun RagQueryLog métier n'est créé
            rag_space=space,
            entrypoint="internal",
            trace_sink=captured.update,
        )

        prod_candidates = [c.get("chunk_id") for c in (files["candidates"].get("candidates") or [])]
        prod_selected = [
            s.get("chunk_id") for s in ((files["selected"] or {}).get("sources") or [])
        ]
        dev_candidates = [c.get("chunk_id") for c in (captured.get("candidates") or [])]
        dev_selected = [s.get("chunk_id") for s in (result.get("sources") or [])]

        prod_rank = {cid: i + 1 for i, cid in enumerate(prod_candidates)}
        dev_rank = {cid: i + 1 for i, cid in enumerate(dev_candidates)}

        return {
            "production": {
                "candidate_ids": prod_candidates,
                "selected_ids": prod_selected,
                "answer_kind": (files["generation"] or {}).get("policy"),
            },
            "dev": {
                "candidate_ids": dev_candidates,
                "selected_ids": dev_selected,
                "answer_kind": result.get("answer_kind") or result.get("status"),
            },
            "differences": {
                "missing_candidates": [c for c in prod_candidates if c not in dev_rank],
                "new_candidates": [c for c in dev_candidates if c not in prod_rank],
                "rank_changes": [
                    {"chunk_id": cid, "production": prod_rank[cid], "dev": dev_rank[cid]}
                    for cid in prod_rank
                    if cid in dev_rank and prod_rank[cid] != dev_rank[cid]
                ],
                "selection_changes": {
                    "added": [c for c in dev_selected if c not in prod_selected],
                    "removed": [c for c in prod_selected if c not in dev_selected],
                },
                "answer_kind_changed": (
                    (result.get("answer_kind") or result.get("status"))
                    != (files["generation"] or {}).get("policy")
                ),
            },
            # Le retrieval est déterministe ; dès qu'un LLM entre en jeu, il ne l'est plus.
            "deterministic": mode == MODE_RETRIEVAL,
        }

    # ------------------------------------------------------------------
    def _render(self, report: dict) -> None:
        diff = report.get("differences") or {}
        self.stdout.write("")
        self.stdout.write(f"Incident   : {report.get('incident_id')}")
        self.stdout.write(f"Mode       : {report.get('mode')} (déterministe={report.get('deterministic')})")
        if not report.get("source_capture_is_exact", True):
            self.stdout.write(
                self.style.WARNING(
                    "⚠ La référence « production » vient d'une capture NON exacte "
                    f"({report.get('source_capture_mode')}) : les écarts ci-dessous "
                    "peuvent venir de la capture, pas du code."
                )
            )
        for key in ("missing_candidates", "new_candidates", "rank_changes"):
            value = diff.get(key)
            if value:
                self.stdout.write(f"  {key:20s}: {len(value)}")

        # Formulation IMPOSÉE : la trace observe le pool POST-FUSION. Écrire
        # « jamais candidat » serait une conclusion qu'elle ne soutient pas —
        # un chunk peut avoir été écarté par un seuil du CandidateRetriever, en
        # amont du point de capture.
        if report.get("mode") != "prompt-only":
            self.stdout.write(
                "  Lecture : un chunk absent de « production » est ABSENT DU POOL "
                "POST-FUSION — écarté avant ou pendant le rappel initial. L'étape "
                "exacte (FTS, vectoriel, seuil, périmètre, indexation) n'est pas "
                "observable à ce point de capture."
            )
        if diff.get("selection_changes"):
            self.stdout.write(f"  selection added     : {diff['selection_changes'].get('added')}")
            self.stdout.write(f"  selection removed   : {diff['selection_changes'].get('removed')}")
        if diff.get("answer_kind_changed"):
            self.stdout.write(
                self.style.SUCCESS("  la politique de réponse a CHANGÉ (souvent le signe d'une correction)")
            )
