-- Episodic vs semantic memory: 'fact' = timeless knowledge about them,
-- 'episode' = a dated life event that fades unless it consolidates into a fact.
-- Replaces the FYI store. Run BEFORE deploying episode code.
alter table brain_entries add column if not exists kind text default 'fact';
create index if not exists brain_entries_kind_idx on brain_entries (kind, status);
