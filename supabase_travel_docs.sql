-- Travel documents. Sep 2026.
--
-- Ansen: "for travel related reminder, always include our passport details in
-- the reminder - we always have to bump it up. and for visa reminder, remember
-- to check what is needed against each of our passports."
--
-- A reminder that must ALWAYS carry a detail cannot depend on recall finding it.
-- Before this, the only passport data lived as prose inside
-- user_summaries(user_id=63756531) — chat-injected but invisible to
-- query_brain, so trip_milestone_brief (the thing that actually writes the
-- pre-trip reminders) could not see it at all. It also had no expiry field, so
-- nothing could check the six-month validity rule that most countries apply.
--
-- One row per document per person, so a visa check can be run per passport.

CREATE TABLE IF NOT EXISTS travel_docs (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    person       text NOT NULL,               -- 'ansen' | 'jess'
    doc_type     text NOT NULL,               -- passport | pass | known_traveler | visa | other
    nationality  text,                        -- drives the visa lookup: 'Singapore', 'United States'
    number       text,
    issued       date,
    expires      date,
    notes        text,
    -- 'missing' is a real state and the useful one: it lets a reminder say
    -- "your passport is not on file" instead of quietly leaving it out.
    status       text NOT NULL DEFAULT 'active',   -- active | expired | missing | superseded
    created_at   timestamptz DEFAULT now(),
    updated_at   timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS travel_docs_person_idx ON travel_docs (person, status);
