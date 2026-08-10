-- Query-path indexes: lineage grouping on the dashboard, per-job event trails,
-- and the cron's time-based events prune.
CREATE INDEX idx_jobs_lineage ON jobs(lineage_id);
CREATE INDEX idx_events_job ON events(job_id);
CREATE INDEX idx_events_ts ON events(ts);
