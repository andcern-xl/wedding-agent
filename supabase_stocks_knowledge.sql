create table if not exists stocks_knowledge (
  id uuid primary key default gen_random_uuid(),
  brief_date date not null unique,
  assets jsonb not null default '[]'::jsonb,
  brief_text text,
  created_at timestamptz default now()
);

create index if not exists stocks_knowledge_date_idx on stocks_knowledge (brief_date desc);
