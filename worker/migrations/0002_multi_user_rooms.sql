CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  password_salt TEXT NOT NULL,
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS sessions_user_id ON sessions(user_id);
CREATE INDEX IF NOT EXISTS sessions_expires_at ON sessions(expires_at);

CREATE TABLE IF NOT EXISTS profiles (
  user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  state_json TEXT NOT NULL,
  backup_json TEXT NOT NULL DEFAULT '',
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS rooms (
  id TEXT PRIMARY KEY,
  code TEXT NOT NULL UNIQUE,
  title TEXT NOT NULL,
  host_user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS room_members (
  room_id TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  joined_at INTEGER NOT NULL,
  PRIMARY KEY(room_id, user_id)
);

CREATE INDEX IF NOT EXISTS room_members_user_id ON room_members(user_id);
