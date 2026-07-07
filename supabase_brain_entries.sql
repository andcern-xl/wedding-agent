-- Structured shared brain vault. Run BEFORE deploying vault code (inert until then).
create table if not exists brain_entries (
    id            uuid primary key default gen_random_uuid(),
    domain        text not null default 'life' check (domain in ('baby','wedding','travel','money','life')),
    fact          text not null,
    fact_date     date not null default current_date,
    status        text not null default 'active' check (status in ('active','superseded')),
    superseded_by uuid references brain_entries(id),
    source        text not null default 'chat',
    created_at    timestamptz not null default now(),
    updated_at    timestamptz not null default now()
);

create index if not exists brain_entries_active_idx on brain_entries (status, domain);
create index if not exists brain_entries_date_idx on brain_entries (fact_date desc);
