create table if not exists grocery_lists (
  id uuid primary key default gen_random_uuid(),
  name text not null default 'Groceries',
  created_by bigint,
  status text not null default 'active',
  created_at timestamptz default now()
);

create table if not exists grocery_items (
  id uuid primary key default gen_random_uuid(),
  list_id uuid references grocery_lists(id) on delete cascade,
  item text not null,
  quantity text,
  added_by bigint,
  done boolean not null default false,
  created_at timestamptz default now()
);

create index if not exists grocery_items_list_idx on grocery_items (list_id);
create index if not exists grocery_lists_status_idx on grocery_lists (status);
