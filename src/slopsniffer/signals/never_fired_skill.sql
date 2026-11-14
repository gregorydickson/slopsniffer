-- Inventory paths are retained, but an identical installed definition has one finding.
-- A worktree, plugin cache version, or symlink copy does not add another unused skill.
WITH unused AS (
    SELECT s.* FROM skills s
    WHERE NOT EXISTS (
        SELECT 1 FROM skill_invocations i JOIN events e USING (event_id)
        WHERE (CAST($since AS TIMESTAMPTZ) IS NULL OR e.ts >= CAST($since AS TIMESTAMPTZ))
          AND (i.skill_name = s.name OR (
              i.skill_name = regexp_replace(s.name, '^.*:', '')
              AND NOT EXISTS (SELECT 1 FROM skills other
                              WHERE other.name <> s.name
                                AND regexp_replace(other.name, '^.*:', '') = i.skill_name)
          ))
    )
)
SELECT min(path) AS session_id, 0.0 AS severity, []::VARCHAR[] AS evidence_event_ids,
       json_object('skill', name, 'scope', arg_min(scope, path),
                   'installations', count(*), 'interpretation', 'inventory_only_not_proven_waste') AS details
FROM unused GROUP BY name, content_hash
