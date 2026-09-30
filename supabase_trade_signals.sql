-- Trade + X signals POSTed in by the Growth Research agent (webhook.py).
-- One row per received signal. The same signal_id from the same source is
-- ignored for 24h after it first arrives (tools/signals.py), so a retrying
-- sender can't double-count a signal toward the brief's convergence bar.
create table if not exists trade_signals (
    id           bigserial primary key,
    source       text not null,
    signal_id    text not null,
    type         text not null check (type in ('crypto', 'stock', 'x_social')),
    symbol       text not null,
    direction    text not null check (direction in ('long', 'short', 'neutral', 'watch')),
    strength     real not null default 0,
    summary      text not null default '',
    proof_urls   jsonb not null default '[]'::jsonb,
    raw          jsonb not null default '{}'::jsonb,
    as_of        timestamptz,
    received_at  timestamptz not null default now()
);

create index if not exists trade_signals_dedupe on trade_signals (source, signal_id, received_at desc);
create index if not exists trade_signals_recent on trade_signals (received_at desc);
