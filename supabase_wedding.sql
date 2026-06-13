-- Wedding drops — stores all notes, screenshots, messages dropped by the couple
create table if not exists wedding_drops (
    id         uuid primary key default gen_random_uuid(),
    ts         timestamptz not null default now(),
    user_id    bigint not null,
    category   text,
    kind       text not null default 'text' check (kind in ('text', 'image')),
    content    text not null
);

create index if not exists wedding_drops_ts_idx on wedding_drops (ts desc);
create index if not exists wedding_drops_category_idx on wedding_drops (category);

-- Wedding memory — locked decisions and notes per category
create table if not exists wedding_memory (
    id       uuid primary key default gen_random_uuid(),
    category text not null,
    field    text not null,   -- 'decisions', 'notes', 'docs'
    value    text not null,
    created_at timestamptz default now()
);

create index if not exists wedding_memory_category_idx on wedding_memory (category);

-- User summaries — compressed per-user memory; user_id=0 = shared brain
create table if not exists user_summaries (
    id            uuid primary key default gen_random_uuid(),
    user_id       bigint unique not null,
    summary       text not null default '',
    message_count int not null default 0,
    updated_at    timestamptz default now()
);

-- Wedding payments — budget tracking
create table if not exists wedding_payments (
    id        uuid primary key default gen_random_uuid(),
    vendor    text,
    amount    numeric,
    currency  text,
    status    text,   -- 'paid', 'deposit', 'owing', 'quote'
    paid_by   text,
    date      text,
    notes     text,
    logged_at timestamptz default now()
);
