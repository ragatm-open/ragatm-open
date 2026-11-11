# Backend (Django)

## Emplacement
- Chemin (relatif racine repo) : `backend`


## Rôle
Backend Django principal : API, sécurité, orchestration RAG, routage LLM, audit et coûts.

## Contenu
- `api/` : endpoints HTTP versionnés
- `apps/` : domaines Django (accounts, chat, documents, rag_core, llm_router…)
- `config/` : settings Django (dev/prod)
- `tests/` : tests (dont règles RAG)
- `media/`, `static/` : fichiers uploadés / assets (selon config)

## Conventions
- Les views/serializers ne contiennent **pas** de logique métier.
- Les décisions (rerank, "je ne sais pas") sont prises côté backend (pas par le LLM).

## Sécurité & contraintes
- Isolation multi-RAG obligatoire (filtrage systématique)
- Logs et audit : minimiser PII, respecter privacy

