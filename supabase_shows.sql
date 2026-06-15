create table if not exists shows (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null,
  show_name text not null,
  venue text,
  show_date date,
  show_time text,
  notes text,
  calendar_added boolean default false,
  created_at timestamptz default now()
);
