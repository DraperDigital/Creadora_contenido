CREATE TABLE jobs (
  id TEXT PRIMARY KEY,
  filename TEXT NOT NULL,
  format TEXT NOT NULL DEFAULT 'short',
  status TEXT NOT NULL DEFAULT 'uploading',
  input_key TEXT NOT NULL,
  output_key TEXT,
  size_bytes INTEGER,
  error TEXT,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  claimed_at INTEGER,
  heartbeat_at INTEGER
);
CREATE INDEX idx_jobs_status_created ON jobs(status, created_at);
