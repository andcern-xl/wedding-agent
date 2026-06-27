create table if not exists goals (
  id uuid primary key default gen_random_uuid(),
  user_id bigint not null,
  title text not null,
  visibility text default 'shared',
  status text default 'active',
  category text,
  created_at timestamptz default now()
);

create table if not exists goal_steps (
  id uuid primary key default gen_random_uuid(),
  goal_id uuid not null references goals(id) on delete cascade,
  title text not null,
  sort_order int default 0,
  status text default 'open',
  blocked_by uuid references goal_steps(id),
  due_date date,
  assigned_to bigint,
  completed_at timestamptz,
  created_at timestamptz default now()
);
