-- spotube-dj worker schema. Apply with:
--   npx wrangler d1 execute spotube-dj --file=schema.sql --remote
--
-- Two tables, on purpose. The taste profile is one JSON blob because that is
-- exactly what the app already writes to ~/.spotube-dj/state.json - it is a
-- backup and a second machine's starting point, not a schema to be queried
-- row by row. The event log is the part that is genuinely relational: it is
-- append-only and it is read "give me everything since the id I last saw".

-- One row per listener profile: the whole state.json, last write wins.
CREATE TABLE IF NOT EXISTS profiles (
  profile     TEXT PRIMARY KEY,
  state       TEXT NOT NULL,             -- JSON: artists, moods, loved, history
  updated_at  INTEGER NOT NULL           -- epoch ms
);

-- What happened while you were listening: play / like / skip / dislike /
-- request / mix. Replayed on another machine so taste follows you.
CREATE TABLE IF NOT EXISTS events (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  profile  TEXT NOT NULL,
  ts       INTEGER NOT NULL,             -- epoch ms
  kind     TEXT NOT NULL,
  payload  TEXT NOT NULL                 -- JSON
);

CREATE INDEX IF NOT EXISTS idx_events_profile_id ON events (profile, id);
CREATE INDEX IF NOT EXISTS idx_events_profile_ts ON events (profile, ts);
