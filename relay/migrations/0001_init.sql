-- 0001_init.sql
-- HopDrop 初始结构，对应方案 v1.4 第 8 节 DDL，未改动任何关键约束。
--
-- 两点说明：
-- 1. journal_mode / foreign_keys / busy_timeout / synchronous 这些是连接级
--    PRAGMA，必须每个连接设置一次，写在这里无效（尤其 journal_mode 在事务内
--    会直接报错）。它们统一在 app/db.py 的 connect() 里设置。
-- 2. schema_version 用 IF NOT EXISTS，因为迁移执行器要先读它才知道该跑哪几版。

CREATE TABLE IF NOT EXISTS schema_version (
  version    INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE rooms (
  id                TEXT PRIMARY KEY,
  name              TEXT NOT NULL,
  owner_secret_hash BLOB NOT NULL,
  rev               INTEGER NOT NULL DEFAULT 0,
  created_at        INTEGER NOT NULL
);

CREATE TABLE devices (
  id           TEXT PRIMARY KEY,
  room_id      TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  name         TEXT NOT NULL,
  ua           TEXT,
  role         TEXT NOT NULL CHECK(role IN ('owner','guest')),
  revoked_at   INTEGER,
  last_seen_at INTEGER,
  created_at   INTEGER NOT NULL
);

CREATE TABLE device_sessions (
  id         TEXT PRIMARY KEY,
  device_id  TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
  token_hash BLOB NOT NULL UNIQUE,
  expires_at INTEGER,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE TABLE boards (
  id          TEXT PRIMARY KEY,
  room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  retention   INTEGER NOT NULL CHECK(retention IN (0,30,60)),
  expires_at  INTEGER,
  status      TEXT NOT NULL CHECK(status IN ('active','archived')),
  is_guest    INTEGER NOT NULL DEFAULT 0 CHECK(is_guest IN (0,1)),
  archived_at INTEGER,
  sort_order  INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL,
  CHECK((retention = 0 AND expires_at IS NULL) OR
        (retention IN (30,60) AND expires_at IS NOT NULL)),
  CHECK((status = 'archived') = (archived_at IS NOT NULL))
);

CREATE TABLE notes (
  id          TEXT PRIMARY KEY,
  room_id     TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  board_id    TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  author_id   TEXT REFERENCES devices(id) ON DELETE SET NULL,
  kind        TEXT NOT NULL CHECK(kind IN ('text','file_ref')),
  content     TEXT NOT NULL,
  pinned      INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
  mutation_id TEXT UNIQUE,
  deleted_at  INTEGER,
  created_at  INTEGER NOT NULL,
  updated_at  INTEGER NOT NULL
);

CREATE TABLE files (
  id           TEXT PRIMARY KEY,
  room_id      TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  board_id     TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  uploader_id  TEXT REFERENCES devices(id) ON DELETE SET NULL,
  display_name TEXT NOT NULL,
  size         INTEGER NOT NULL CHECK(size BETWEEN 1 AND 20971520),
  mime         TEXT,
  sha256       TEXT NOT NULL,
  storage_path TEXT NOT NULL UNIQUE,
  thumb_path   TEXT,
  expires_at   INTEGER NOT NULL,
  deleted_at   INTEGER,
  purged_at    INTEGER,
  created_at   INTEGER NOT NULL
);

CREATE TABLE guest_codes (
  id         TEXT PRIMARY KEY,
  room_id    TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
  board_id   TEXT NOT NULL REFERENCES boards(id) ON DELETE CASCADE,
  code_hash  BLOB NOT NULL UNIQUE,
  max_uses   INTEGER NOT NULL DEFAULT 1,
  used_count INTEGER NOT NULL DEFAULT 0 CHECK(used_count >= 0),
  expires_at INTEGER NOT NULL,
  revoked_at INTEGER,
  created_at INTEGER NOT NULL
);

CREATE INDEX idx_boards_room_status ON boards(room_id, status, sort_order);
CREATE INDEX idx_boards_expire ON boards(status, expires_at);
CREATE UNIQUE INDEX uq_boards_guest ON boards(room_id) WHERE is_guest = 1;
CREATE INDEX idx_notes_board_time ON notes(board_id, created_at DESC);
CREATE INDEX idx_files_room_expire ON files(room_id, expires_at);
CREATE INDEX idx_files_purge ON files(purged_at);
CREATE INDEX idx_guest_expire ON guest_codes(expires_at);

-- 补充索引：会话与设备是每次请求都要查的路径，按 device 和有效期建索引。
CREATE INDEX idx_sessions_token ON device_sessions(token_hash);
CREATE INDEX idx_sessions_device ON device_sessions(device_id, revoked_at);
CREATE INDEX idx_devices_room ON devices(room_id, revoked_at);
CREATE INDEX idx_notes_board_live ON notes(board_id, deleted_at);
