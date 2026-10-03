-- ============================================================================
-- CANONICAL QUERIES — every stack must issue EXACTLY these statements.
-- Parameter binding style may differ ($1 vs :param), but statement text,
-- joins, casts and semantics must be identical across all 7 stacks.
-- Extra queries, extra joins, or extra round-trips are NOT allowed.
-- Offset note: if a driver cannot bind inside OFFSET, compute
--   offset = (page - 1) * 20  in the app and bind it as a single parameter
--   ("LIMIT 20 OFFSET $2") — that substitution is canonical too.
-- ============================================================================

-- Q1 (GET /feed?page=N): home feed, newest 20 posts with author + like count
SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at
ORDER BY p.created_at DESC, p.id DESC
LIMIT 20 OFFSET ($1 - 1) * 20;

-- Q2 (GET /posts/:id): single post with author + like count
SELECT p.id, p.user_id, u.username, p.content, p.created_at,
       COUNT(l.id)::bigint AS like_count
FROM posts p
JOIN users u ON u.id = p.user_id
LEFT JOIN likes l ON l.post_id = p.id
WHERE p.id = $1
GROUP BY p.id, p.user_id, u.username, p.content, p.created_at;

-- Q3 (POST /posts): create post. Response = RETURNING row + "like_count": 0
-- (computed in the app, NOT queried). No other statement for this endpoint.
INSERT INTO posts (user_id, content, created_at)
VALUES ($1, $2, now())
RETURNING id, user_id, content, created_at;

-- Q4 (POST /posts/:id/like): exactly these three statements in this order.
-- 4a: missing post -> 404 {"error":"post not found"}
SELECT 1 FROM posts WHERE id = $1;
-- 4b: idempotent insert
INSERT INTO likes (post_id, user_id, created_at)
VALUES ($1, $2, now())
ON CONFLICT (post_id, user_id) DO NOTHING;
-- 4c: fresh count for the response
SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1;
