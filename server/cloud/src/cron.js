const STALE_HEARTBEAT_MS = 30 * 60 * 1000;
const STALE_UPLOAD_MS = 24 * 60 * 60 * 1000;
const EXPIRY_MS = 30 * 24 * 60 * 60 * 1000;
const EVENTS_TTL_MS = 30 * 24 * 60 * 60 * 1000;
// After this many stale-heartbeat requeue cycles a job is considered genuinely
// stuck (repeated timeout/crash, or the machine died) and is terminalized as
// 'failed' with a human error, instead of oscillating processing<->queued
// forever. Each cycle is >= STALE_HEARTBEAT_MS, so this is a slow, safe backstop
// -- the fast path is the local watcher marking failed the moment it sees a
// killed job. A job that keeps heart-beating never goes stale and never counts.
const MAX_REQUEUES = 3;

export async function runCron(env, now = Date.now()) {
  const cancel = await env.DB.prepare(
    `UPDATE jobs SET status='canceled', cancel_requested=0, stage=NULL, stage_detail=NULL, updated_at=?
     WHERE status IN ('claimed','processing','publishing') AND (heartbeat_at IS NULL OR heartbeat_at < ?)
     AND cancel_requested=1`,
  ).bind(now, now - STALE_HEARTBEAT_MS).run();

  // A job that has already been requeued MAX_REQUEUES times and is stale again is
  // genuinely stuck -> fail it with a human error so the dashboard stops showing
  // "processing"/"queued" forever. Runs BEFORE the requeue; the two WHERE clauses
  // are mutually exclusive on requeue_count so a job is either failed or requeued,
  // never both, in one cron tick.
  const failStuck = await env.DB.prepare(
    `UPDATE jobs SET status='failed',
       error='el proceso se detuvo sin terminar (timeout o caida del equipo); reintenta el video',
       stage=NULL, stage_detail=NULL, cancel_requested=0, claimed_at=NULL, heartbeat_at=NULL, updated_at=?
     WHERE status IN ('claimed','processing','publishing')
       AND (heartbeat_at IS NULL OR heartbeat_at < ?)
       AND requeue_count >= ?`,
  ).bind(now, now - STALE_HEARTBEAT_MS, MAX_REQUEUES).run();

  const requeue = await env.DB.prepare(
    `UPDATE jobs SET status='queued', requeue_count=requeue_count+1,
       claimed_at=NULL, heartbeat_at=NULL, updated_at=?
     WHERE status IN ('claimed','processing','publishing')
       AND (heartbeat_at IS NULL OR heartbeat_at < ?)
       AND requeue_count < ?`,
  ).bind(now, now - STALE_HEARTBEAT_MS, MAX_REQUEUES).run();

  const stale = await env.DB.prepare(
    "SELECT id, input_key FROM jobs WHERE status='uploading' AND created_at < ?",
  ).bind(now - STALE_UPLOAD_MS).all();
  for (const row of stale.results) {
    try {
      await env.MEDIA.delete(row.input_key);
    } catch {
      // best-effort: a missing object must not block the purge
    }
    await env.DB.prepare("DELETE FROM jobs WHERE id=?").bind(row.id).run();
  }

  // Every terminal status expires here: done/failed, canceled (whose input
  // object would otherwise leak forever), and rejected (terminal like failed).
  const expired = await env.DB.prepare(
    "SELECT id, input_key, output_key FROM jobs WHERE status IN ('done','failed','canceled','rejected') AND updated_at < ?",
  ).bind(now - EXPIRY_MS).all();
  for (const row of expired.results) {
    try {
      await env.MEDIA.delete(row.input_key);
    } catch (e) {
      console.log("expiry input cleanup failed:", e);
    }
    if (row.output_key) {
      try {
        await env.MEDIA.delete(row.output_key);
      } catch (e) {
        console.log("expiry output cleanup failed:", e);
      }
    }
    let cursor;
    do {
      const listing = await env.MEDIA.list({ prefix: `outbox/${row.id}/`, cursor });
      for (const obj of listing.objects) {
        try {
          await env.MEDIA.delete(obj.key);
        } catch (e) {
          console.log("expiry output cleanup failed:", e);
        }
      }
      cursor = listing.truncated ? listing.cursor : undefined;
    } while (cursor);
    await env.DB.prepare("DELETE FROM jobs WHERE id=?").bind(row.id).run();
  }

  const eventsPruned = await env.DB.prepare(
    "DELETE FROM events WHERE ts < ?",
  ).bind(now - EVENTS_TTL_MS).run();

  // Login rate-limit hygiene: rows a day past their last failure are dead.
  try {
    await env.DB.prepare(
      "DELETE FROM login_attempts WHERE last_fail_at < ?",
    ).bind(now - 24 * 60 * 60 * 1000).run();
  } catch (e) {
    console.log("login attempts prune failed:", e);
  }

  return {
    requeued: requeue.meta.changes,
    failed_stuck: failStuck.meta.changes,
    purged: stale.results.length,
    expired: expired.results.length,
    canceled: cancel.meta.changes,
    events_pruned: eventsPruned.meta.changes,
  };
}
