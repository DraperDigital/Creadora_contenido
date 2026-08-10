-- Bounded requeue counter so a job that repeatedly goes stale (timeout / crash /
-- dead machine) is eventually terminalized as 'failed' by the cron instead of
-- oscillating processing<->queued forever. The dashboard already renders a
-- 'failed' status + error message; this is what finally lets the cron reach it.
ALTER TABLE jobs ADD COLUMN requeue_count INTEGER NOT NULL DEFAULT 0;
