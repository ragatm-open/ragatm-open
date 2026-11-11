# Approvisionnement des modèles

L'approvisionnement est une **phase explicite**, distincte de l'exécution nominale :

- **Approvisionnement** : téléchargements initiaux autorisés, lancés par l'exploitant, avec
  l'override `compose.provision.yml` qui ajoute un réseau de sortie aux seuls services `ollama`
  et `ai-gateway` (ce dernier partage son espace réseau avec le reranker TEI).
- **Exécution nominale** : aucun accès Internet ; les réseaux `app`, `data`, `ai` sont internes ;
  la Gateway ne télécharge jamais rien pendant une requête (un modèle enregistré mais absent
  répond `503 model_not_installed`, non réessayable).

```bash
scripts/provision-models.sh              # profil CPU / 8 Go
scripts/provision-models.sh --with-12gb  # + qwen2.5:7b-instruct (profil 12 Go, NON qualifié)
```

Le script tire les modèles dans les volumes `ollama_data` et `tei_data`, attend la disponibilité
du reranker, relève l'inventaire (`state/provision-manifest.json` : digests Ollama, identité TEI)
puis arrête les services d'approvisionnement. `scripts/up.sh` redémarre ensuite la pile en mode
nominal (sans réseau de sortie, `HF_HUB_OFFLINE=1` pour TEI).

## Registre Compact (AI Gateway Open, `app/models_registry.yaml`)

| Nom logique | Rôle | Artefact | Digest / révision attendu | Licence | État |
|---|---|---|---|---|---|
| `fr-en-retrieval` | embeddings (défaut) | `granite-embedding:278m` | `1a37926bf842…95c552` | Apache-2.0 | experimental — identité vérifiée lors de la qualification |
| `legacy-768` | embeddings (index existants) | `nomic-embed-text` | `0a109f422b47…c45e59f` | Apache-2.0 | candidate — non installé en Compact |
| `compact-generation-8` | génération CPU / 8 Go | `qwen2.5:1.5b-instruct` | `65ec06548149…ddc57b` | Apache-2.0 | experimental — profil de qualification fonctionnelle (pas une qualification de qualité documentaire) |
| `compact-generation` | génération 12 Go | `qwen2.5:7b-instruct` | aucun digest relevé | Apache-2.0 | candidate — **non installé, non qualifié** ; jamais un repli |
| `rerank-default` | reranking | `BAAI/bge-reranker-v2-m3` | révision `953dc6f6…` | Apache-2.0 | experimental — identité vérifiée par `/health/rerank` |

Un digest observé différent du digest attendu rend le profil « non gouverné » : la Gateway refuse
alors de servir des embeddings estampillés `pipeline_id` et RAGatm refuse tout vecteur sans
`identity_verified: true`. Aucune réindexation implicite n'a lieu.

## Passage au profil 12 Go (hors qualification interne)

1. `scripts/provision-models.sh --with-12gb` sur une machine disposant d'un GPU 12 Go isolé.
2. Relever le digest (`docker compose exec ollama ollama list`) et le reporter dans
   `../../ai-gateway-open/app/models_registry.yaml` (`compact-generation.digest`).
3. Mesurer VRAM à froid / chargé / pic, RAM, premier token, latence, concurrence, saturations.
4. Démarrer avec `docker compose -f compose.yml -f compose.gpu-12gb.yml up -d`.

Sans ces mesures réelles, le profil reste `candidate` et `COMPACT_12GB_VERDICT = NOT_EXECUTED`.
