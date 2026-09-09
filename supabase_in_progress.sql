-- "On it" — the state between untouched and done. Sep 2026.
--
-- Ansen: "should there be an option for 'in progress/ working on it', then it'll
-- check back to ask for follow ups"
--
-- A task had only untouched, deferred, done or settled. Deferring an item you
-- have actually started is wrong twice over: the follow-up re-asks the original
-- question ("get a florist quote") when the real question is what came back, and
-- the settle pass counts it as stale when it is anything but.
--
-- in_progress_since records when they said they were on it. iceboxed_until
-- continues to hold the check-back date, so nothing nags in between.

ALTER TABLE daily_tasks ADD COLUMN IF NOT EXISTS in_progress_since date;

CREATE INDEX IF NOT EXISTS daily_tasks_in_progress_idx
    ON daily_tasks (in_progress_since)
    WHERE in_progress_since IS NOT NULL;
