create table if not exists proactive_state (
  user_id bigint primary key,
  last_output text,
  last_run_date text,
  updated_at timestamptz default now()
);
