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
  created_at timestamptz default now()
);
