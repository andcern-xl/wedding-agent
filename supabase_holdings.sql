-- Investment holdings (the "finances" layer). Run BEFORE deploying holdings code.
create table if not exists holdings (
    id          uuid primary key default gen_random_uuid(),
    owner       text not null default 'joint' check (owner in ('ansen','jess','joint')),
    asset       text not null,
    ticker      text,
    asset_type  text not null default 'stock' check (asset_type in ('stock','etf','fund','crypto','cash','other')),
    platform    text,
    units       numeric,
    avg_cost    numeric,
    value       numeric,
    currency    text not null default 'SGD',
    as_of       date not null default current_date,
    notes       text,
    status      text not null default 'active' check (status in ('active','closed')),
    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now()
);

create index if not exists holdings_active_idx on holdings (status, owner);
