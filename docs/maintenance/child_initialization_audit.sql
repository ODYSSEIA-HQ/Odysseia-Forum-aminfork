-- 仅审计，不修复：必须由已授权的只读连接运行。
-- 不读取正文、账户凭据；候选结果不是自动回填授权。
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '15s';
SELECT w.id AS work_id, t.id AS thread_row_id,
       t.guild_id::text AS guild_id, t.thread_id::text AS thread_id,
       t.initializing, t.deleted_at, t.deletion_reason,
       r.id AS release_task_id, r.status AS release_status,
       r.payload ->> 'source' AS policy_source,
       r.payload ->> 'active' AS policy_active,
       (r.work_id = t.work_id
        AND r.payload ->> 'guild_id' = t.guild_id::text
        AND r.payload ->> 'thread_id' = t.thread_id::text
        AND r.payload ->> 'active' = 'false'
        AND r.payload ->> 'source' = 'forum_initializing'
        AND COALESCE(r.payload ->> 'target_user_id', '') = '') AS scope_matches,
       (SELECT COUNT(*)
          FROM json_array_elements_text(
              CASE WHEN json_typeof(r.payload -> 'depends_on') = 'array'
                   THEN r.payload -> 'depends_on' ELSE '[]'::json END
          ) AS dependency(id)
          LEFT JOIN work_task d ON d.id::text = dependency.id
         WHERE d.id IS NULL OR d.status <> 'done') AS incomplete_dependencies
FROM thread t
JOIN work w ON w.id = t.work_id
LEFT JOIN work_task r
  ON r.dedupe_key = 'release-child:' || t.id::text AND r.kind = 'policy'
WHERE t.platform = 'fluxer' AND t.role = 'child' AND t.initializing IS TRUE
ORDER BY w.id, t.id;
ROLLBACK;
