-- Episodic vs semantic memory: 'fact' = timeless knowledge about them,
-- 'episode' = a dated life event that fades unless it consolidates into a fact.
-- Replaces the FYI store. Run BEFORE deploying episode code.
alter table brain_entries add column if not exists kind text default 'fact';
alter table brain_entries drop constraint if exists brain_entries_kind_check;
alter table brain_entries add constraint brain_entries_kind_check check (kind in ('fact', 'episode'));
create index if not exists brain_entries_kind_idx on brain_entries (kind, status);
