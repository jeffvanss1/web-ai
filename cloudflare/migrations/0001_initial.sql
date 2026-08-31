CREATE TABLE IF NOT EXISTS tracks (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  artist TEXT NOT NULL DEFAULT '',
  duration INTEGER NOT NULL DEFAULT 0,
  thumbnail TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT 'youtube-music',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS likes (
  user_id TEXT NOT NULL,
  track_id TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, track_id),
  FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS history (
  user_id TEXT NOT NULL,
  track_id TEXT NOT NULL,
  played_at INTEGER NOT NULL,
  PRIMARY KEY (user_id, track_id, played_at),
  FOREIGN KEY (track_id) REFERENCES tracks(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS history_user_time ON history(user_id, played_at DESC);
CREATE INDEX IF NOT EXISTS likes_user_time ON likes(user_id, created_at DESC);
