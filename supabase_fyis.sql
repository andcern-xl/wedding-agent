-- Run this in the Supabase SQL editor to enable FYIs

CREATE TABLE IF NOT EXISTS fyis (
  id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id bigint NOT NULL,
  content text NOT NULL,
  category text,
  created_at timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fyis_created_at ON fyis (created_at DESC);
