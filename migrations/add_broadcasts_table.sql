-- Create broadcasts table for tracking promo campaigns (2026-05-05)
CREATE TABLE IF NOT EXISTS broadcasts (
    id SERIAL PRIMARY KEY,
    source VARCHAR(32) NOT NULL,           -- 'velum', 'aurum', 'neyra'
    promo_code VARCHAR(64) NOT NULL,
    total_recipients INTEGER NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0,
    blocked_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMP NULL,
    duration_seconds INTEGER NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'completed', -- running, completed, failed
    log_file VARCHAR(256) NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_broadcasts_source ON broadcasts(source);
CREATE INDEX IF NOT EXISTS idx_broadcasts_promo_code ON broadcasts(promo_code);
CREATE INDEX IF NOT EXISTS idx_broadcasts_started_at ON broadcasts(started_at DESC);

-- Optional: Add to bot_config for quick access
INSERT INTO bot_config (key, value, description) VALUES
  ('last_velum_broadcast_id', '0', 'ID последней рассылки Velum'),
  ('last_aurum_broadcast_id', '0', 'ID последней рассылки Aurum')
ON CONFLICT (key) DO NOTHING;

COMMIT;
