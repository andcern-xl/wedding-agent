-- Baby registry events POSTed in by the baby-registry bot (webhook.py,
-- POST /webhooks/registry). Separate from trade_signals: different sender,
-- different secret, different shape. The same event_id from the same source
-- is ignored for 24h after it first arrives (tools/registry.py).
create table if not exists registry_events (
    id           bigserial primary key,
    source       text not null,
    event_id     text not null,
    type         text not null check (type in ('research_update', 'list_update', 'price_update',
                                               'timeline_update', 'question', 'note')),
    summary      text not null default '',
    priority     text not null check (priority in ('P0', 'P1', 'P2', 'info')),
    payload      jsonb not null default '{}'::jsonb,
    links        jsonb not null default '[]'::jsonb,
    as_of        timestamptz,
    received_at  timestamptz not null default now()
);

create index if not exists registry_events_dedupe on registry_events (source, event_id, received_at desc);
create index if not exists registry_events_recent on registry_events (received_at desc);
