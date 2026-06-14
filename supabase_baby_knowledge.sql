create table if not exists baby_knowledge (
  id          uuid primary key default gen_random_uuid(),
  user_id     bigint,
  summary     text not null,
  raw_text    text,
  tags        text[] default '{}',
  source      text default 'screenshot',
  created_at  timestamptz default now()
);
