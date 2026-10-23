# Library query plans — 2026-10-23

Checked SQLite and a throwaway PostgreSQL 16 instance. No application/developer database was queried or modified.
This is a synthetic query comparison, not an end-to-end production benchmark.

## Workload

100,000 rows per table, 20 users, 50 projects per user, 20% ungrouped files/conversations;
about 14% image MIME types and 3% starred files. Query one user's 5,000 rows and limit lists to 50.
Run ANALYZE after each index stage; PostgreSQL VACUUM ANALYZE after loading to enable realistic index-only scans.
Elapsed figures below are mean client query times over ten warm repetitions (ms), and are illustrative.
The timestamp distribution deliberately includes stretches of non-image rows to expose filtering work.

Existing indexes: user_id, project_id, and (user_id, created_at); conversations use the corresponding updated_at ordering in the application.
The count queries do not depend on the ordering timestamp. New indexes are amended into the unreleased 0008 migration:

```sql
CREATE INDEX ix_files_user_starred ON files (user_id, starred_at)
  WHERE starred_at IS NOT NULL;
CREATE INDEX ix_files_user_project_created ON files (user_id, project_id, created_at);
CREATE INDEX ix_files_images_user_created ON files (user_id, created_at)
  WHERE content_type LIKE 'image/%';
CREATE INDEX ix_conversations_user_project_live ON conversations (user_id, project_id)
  WHERE deleted_at IS NULL AND project_id IS NOT NULL;
```

## Queries and evidence

- **All:** `WHERE user_id = :user ORDER BY created_at DESC LIMIT 50` continues to use `ix_files_user_created` on both engines. No additional index needed.
- **Starred:** `WHERE user_id = :user AND starred_at IS NOT NULL ORDER BY starred_at DESC LIMIT 50` changes from scanning the user's files and sorting to `ix_files_user_starred` in order. SQLite about 7.3 → 0.14 ms; PostgreSQL about 0.78 → 0.15 ms.
- **Images:** `WHERE user_id = :user AND content_type LIKE 'image/%' ORDER BY created_at DESC LIMIT 50` changes to `ix_files_images_user_created` in order. SQLite about 6.7 → 0.13 ms. PostgreSQL EXPLAIN ANALYZE: about 0.87 → 0.055 ms, buffers about 4363 → 52, with 4285 non-image rows previously removed by the filter.
- **Project:** `WHERE user_id = :user AND project_id = :project ORDER BY created_at DESC LIMIT 50` uses `ix_files_user_project_created`. SQLite drops its temporary ORDER BY B-tree (about 0.21 → 0.13 ms). PostgreSQL replaces a BitmapAnd across the single-column indexes with one composite bitmap scan; for the small 65-row project it still chooses a cheap sort (about 0.28 → 0.20 ms).
- **q:** `WHERE user_id = :user AND filename ILIKE '%report%' ORDER BY created_at DESC LIMIT 50` (SQLite uses lower/LIKE) remains bounded by the user index. A normal filename B-tree cannot accelerate the leading wildcard. No speculative text/trigram index or search-semantic change added.
- **File counts:** `SELECT project_id, count(*) FROM files WHERE user_id = :user AND project_id IS NOT NULL GROUP BY project_id` uses the covering `ix_files_user_project_created` on SQLite (about 8.3 → 0.56 ms), and an Index Only Scan on PostgreSQL (about 0.97 → 0.53 ms).
- **Conversation counts:** same grouping with `deleted_at IS NULL` uses `ix_conversations_user_project_live`. SQLite removes its GROUP BY temporary B-tree (about 8.6 → 0.68 ms); PostgreSQL uses an Index Only Scan (about 1.07 → 0.40 ms).

The project detail query now restricts each count subquery to that project, and the project list excludes NULL project groups that can never join a project.

SQLite requires a literal `LIKE 'image/%'` predicate to match this partial index; a bound `LIKE ?` does not match it.
The API uses SQLAlchemy's constant literal_column for that fixed MIME prefix. User-provided search text stays bound and escaped.
The original all-files ordering index is retained: the project composite cannot provide creation-time ordering across projects.
These four indexes add write/storage cost only for the measured paths (starred/images/live-project conversations are partial).

Migration upgrade/downgrade, existing referenced data, and metadata drift are checked on both database engines by the normal migration suite.
