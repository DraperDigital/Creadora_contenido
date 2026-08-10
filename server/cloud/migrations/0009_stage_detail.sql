-- Fine-grained progress for the dashboard ("Animando escena n de t"): the
-- agent's status POST may carry a small JSON object (whitelisted keys
-- scene/total/pct) which listJobs returns alongside the coarse stage.
ALTER TABLE jobs ADD COLUMN stage_detail TEXT;
