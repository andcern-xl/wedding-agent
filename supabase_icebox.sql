-- Icebox/backlog for stale tasks. Run BEFORE deploying icebox code.
alter table daily_tasks add column if not exists iceboxed_until date;
alter table daily_tasks add column if not exists icebox_offered_at date;
