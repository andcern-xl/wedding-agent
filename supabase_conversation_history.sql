create table if not exists conversation_history (
  chat_id bigint primary key,
  messages jsonb not null default '[]'::jsonb,
  updated_at timestamptz default now()
);
