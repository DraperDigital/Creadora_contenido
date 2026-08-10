-- Login rate limiting: per-IP failed-attempt counter. 5 failures lock the IP
-- out for 15 minutes; a successful login clears the row. The cron prunes rows
-- older than a day.
CREATE TABLE login_attempts (
  ip TEXT PRIMARY KEY,
  fail_count INTEGER NOT NULL DEFAULT 0,
  last_fail_at INTEGER NOT NULL
);
