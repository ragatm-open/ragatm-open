API HTTP — Documentation consolidée

======================================================================
Emplacement
======================================================================

- backend/api
- <racine du dépôt>

======================================================================
Rôle
======================================================================

Couche HTTP exposant les endpoints consommés par le frontend et les intégrations.

======================================================================
Responsabilités
======================================================================

- validation des entrées (serializers)
- orchestration vers les services applicatifs
- aucune logique métier lourde
- aucun accès direct DB

======================================================================
Organisation
======================================================================

api/
└── v1/
    ├── chat/
    │   ├── serializers.py
    │   └── views.py
    └── documents/
        ├── serializers.py
        └── views.py

======================================================================
TABLEAU 1 — FONCTIONS (views uniquement)
======================================================================

| Fichier  | Emplacement        | Fonction | Endpoints                                 | Description synthétique                                   | Inputs                                  | Outputs |
|----------|--------------------|----------|--------------------------------------------|------------------------------------------------------------|------------------------------------------|---------|
| views.py | api/v1/chat        | post     | POST /api/v1/chat/query                    | Traite une requête chat et délègue au service RAG           | rag_space_id, question                   | Response (answer / "Je ne sais pas.") |
| views.py | api/v1/documents   | post     | POST /api/v1/documents/upload<br>POST /api/v1/documents/ingest | Ingestion documentaire (texte ou fichier)                  | rag_space_id, file ou text               | document_id, status, chunks_created |

======================================================================
TABLEAU 2 — CLASSES
======================================================================

| Classe                  | Fichier        | Emplacement        | Endpoints                                 | Rôle                              | Inputs principaux             | Outputs |
|-------------------------|---------------|--------------------|--------------------------------------------|-----------------------------------|-------------------------------|---------|
| ChatRequestSerializer   | serializers.py| api/v1/chat        | POST /api/v1/chat/query                    | Validation requête chat            | rag_space_id, question        | données validées |
| ChatView                | views.py      | api/v1/chat        | POST /api/v1/chat/query                    | Endpoint chat HTTP                 | request                       | Response |
| DocumentUploadSerializer| serializers.py| api/v1/documents   | POST /api/v1/documents/upload              | Validation upload                  | rag_space_id, file            | données validées |
| DocumentUploadView      | views.py      | api/v1/documents   | POST /api/v1/documents/upload<br>POST /api/v1/documents/ingest | Endpoint ingestion                 | request                       | document_id, status |
