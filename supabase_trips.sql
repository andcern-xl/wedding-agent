create table if not exists trips (
  id uuid primary key default gen_random_uuid(),
  destination text not null,
  country text,
  start_date date,
  end_date date,
  status text default 'planning',
  visa_ansen text,
  visa_jess text,
  notes text,
  visibility text default 'shared',
  created_at timestamptz default now()
);

-- Migration: add visibility if table already exists
alter table trips add column if not exists visibility text default 'shared';
