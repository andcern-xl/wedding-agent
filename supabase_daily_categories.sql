-- Run this in Supabase SQL editor
create table if not exists daily_categories (
    id          uuid primary key default gen_random_uuid(),
    slug        text unique not null,
    name        text not null,
    emoji       text not null default '📌',
    description text default '',
    created_by  bigint,
    created_at  timestamptz default now()
);
