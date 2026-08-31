CREATE TABLE IF NOT EXISTS queue (
  user_id TEXT NOT NULL,
  position INTEGER NOT NULL,
  track_id TEXT NOT NULL,
  added_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, position),
  FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS queue_user_position ON queue(user_id, position);
