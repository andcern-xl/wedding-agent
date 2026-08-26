-- Terminal state for tasks. Aug 2026.
--
-- Until now a task had only two states, open and done, so nothing could ever
-- conclude "this is dead". The icebox defers and then RE-OFFERS after
-- reoffer_days, which is why an ignored task came back forever: 5 of 13 open
-- tasks were already done, and two were tied to trips that had finished.
--
-- settled_at is the terminal state. settled_reason records why, because a task
-- that vanishes with no record is the same bug as a fact deleted with no record
-- (see CLAUDE.md, "Nothing becomes invisible without a record and a reverse
-- gear"). Clearing settled_at brings it straight back.

ALTER TABLE daily_tasks ADD COLUMN IF NOT EXISTS settled_at   date;
ALTER TABLE daily_tasks ADD COLUMN IF NOT EXISTS settled_reason text;

-- Every reader goes through get_tasks, which filters on this.
CREATE INDEX IF NOT EXISTS daily_tasks_settled_idx
    ON daily_tasks (settled_at)
    WHERE settled_at IS NULL;
