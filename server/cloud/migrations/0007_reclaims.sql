-- Per-version delete disk reclaim: when the dashboard deletes a job that has a
-- local run, its run id is queued here for the PC agent to reclaim (remove that
-- version's working + output folders). The agent drains this queue and acks;
-- best-effort, never blocks the D1/R2 delete.
CREATE TABLE reclaims (
  run_id TEXT PRIMARY KEY,
  created_at INTEGER NOT NULL
);
