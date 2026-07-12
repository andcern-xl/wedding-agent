-- Thread ledger: who we're waiting on, per topic, with dated contacts.
-- Kills the "last contact was Jul 11" hallucination class — day-counts come
-- from here or they don't get said. Run BEFORE deploying thread code.
create table if not exists threads (
  id uuid primary key default gen_random_uuid(),
  person text not null,
  topic text not null,
  domain text default 'life',
  status text default 'open',            -- open | waiting_them | waiting_us | resolved
  last_contact date,
  last_direction text,                   -- outbound | inbound
  last_note text,
  created_at timestamptz default now(),
  updated_at timestamptz default now()
);
create index if not exists threads_status_idx on threads (status);
