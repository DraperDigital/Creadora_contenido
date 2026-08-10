-- Per-video version history: an edit becomes a new row (version) in the same
-- lineage (card) instead of overwriting the original.
ALTER TABLE jobs ADD COLUMN lineage_id TEXT;
ALTER TABLE jobs ADD COLUMN version_no INTEGER;
ALTER TABLE jobs ADD COLUMN parent_id TEXT;
ALTER TABLE jobs ADD COLUMN parent_run_id TEXT;
