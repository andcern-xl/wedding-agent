-- Run this in the Supabase SQL editor to enable scheduled notifications

CREATE TABLE IF NOT EXISTS scheduled_notifications (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id bigint NOT NULL,
  message text NOT NULL,
  scheduled_at timestamptz NOT NULL,
  sent boolean DEFAULT false,
  recurrence text DEFAULT 'none' CHECK (recurrence IN ('none', 'daily', 'weekly')),
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_notifications_pending
  ON scheduled_notifications (sent, scheduled_at)
  WHERE sent = false;
