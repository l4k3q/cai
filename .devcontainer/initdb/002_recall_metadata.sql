-- Recall-test metadata for seed questions. Safe to run repeatedly.
ALTER TABLE questions
    ADD COLUMN IF NOT EXISTS recall_metadata JSONB NOT NULL DEFAULT '{}'::jsonb;
