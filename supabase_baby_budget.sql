create table if not exists baby_budget (
  id        uuid primary key default gen_random_uuid(),
  item      text not null,
  category  text,
  amount    numeric,
  currency  text default 'SGD',
  status    text default 'planned',
  notes     text,
  logged_at timestamptz default now()
);
