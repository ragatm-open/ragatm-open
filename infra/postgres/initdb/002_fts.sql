-- 002_fts.sql
-- FTS helpers / indexes (safe on fresh DB before migrations)

-- Si tu as besoin d'une extension, garde-la (au besoin)
-- CREATE EXTENSION IF NOT EXISTS unaccent;

DO $$
BEGIN
  -- Table rag_chunk (Django: apps_rag_core_chunk => table probable: rag_chunk si tu l'as forcée,
  -- sinon ajuste le nom réel de table)
  IF to_regclass('public.rag_chunk') IS NOT NULL THEN
    EXECUTE $q$
      CREATE INDEX IF NOT EXISTS rag_chunk_text_fts_simple_gin
      ON rag_chunk
      USING GIN (to_tsvector('simple', text));
    $q$;
  END IF;

  -- Si tu as d'autres tables/index FTS, répète le pattern.
END
$$;
