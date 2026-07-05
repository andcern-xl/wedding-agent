-- Run this in Supabase SQL editor BEFORE deploying check-in code
create table if not exists check_ins (
    id           uuid primary key default gen_random_uuid(),
    created_by   bigint not null,           -- user the agent was working for when it asked
    audience     text not null default 'me' check (audience in ('me', 'both')),
    question     text not null,
    context      text default '',           -- one-line why-this-matters, shown on the card
    category     text not null default 'life' check (category in ('baby', 'wedding', 'life')),
    options      jsonb not null,            -- [{"label": str, "action": "save_decision|create_task|remind|dismiss", "payload": {...}}]
    status       text not null default 'open' check (status in ('open', 'answered', 'snoozed', 'dismissed', 'expired')),
    answer       text,
    answered_by  bigint,
    snooze_until date,
    created_at   timestamptz not null default now(),
    answered_at  timestamptz
);

create index if not exists check_ins_status_idx on check_ins (status);
create index if not exists check_ins_created_by_idx on check_ins (created_by);
