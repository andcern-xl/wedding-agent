-- Per-loop delta state: one row per (loop, user). user_id = 0 for couple-wide loops.
-- Generalizes proactive_state; that table stays for one release as rollback.
create table if not exists loop_state (
    loop_name     text not null,
    user_id       bigint not null,
    last_output   text,
    last_run_date text,
    updated_at    timestamptz not null default now(),
    primary key (loop_name, user_id)
);

-- Backfill existing proactive check state.
insert into loop_state (loop_name, user_id, last_output, last_run_date)
select 'proactive_check', user_id, last_output, last_run_date from proactive_state
on conflict (loop_name, user_id) do nothing;
