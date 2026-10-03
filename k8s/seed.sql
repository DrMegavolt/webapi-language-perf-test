-- langperf seed: tiny Twitter-style API dataset
-- 50,000 users / 500,000 posts / ~2M likes (matches the reference screenshot)

DROP TABLE IF EXISTS likes;
DROP TABLE IF EXISTS posts;
DROP TABLE IF EXISTS users;

CREATE TABLE users (
  id         BIGSERIAL PRIMARY KEY,
  username   TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE posts (
  id         BIGSERIAL PRIMARY KEY,
  user_id    BIGINT      NOT NULL REFERENCES users(id),
  content    TEXT        NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE likes (
  id         BIGSERIAL PRIMARY KEY,
  post_id    BIGINT      NOT NULL REFERENCES posts(id),
  user_id    BIGINT      NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT likes_post_user_key UNIQUE (post_id, user_id)
);

CREATE INDEX posts_created_at_idx ON posts (created_at DESC, id DESC);
CREATE INDEX posts_user_id_idx    ON posts (user_id);
CREATE INDEX likes_post_id_idx    ON likes (post_id);
CREATE INDEX likes_user_id_idx    ON likes (user_id);

INSERT INTO users (username, created_at)
SELECT 'user_' || g, now() - (random() * interval '365 days')
FROM generate_series(1, 50000) AS g;

INSERT INTO posts (user_id, content, created_at)
SELECT
  (1 + floor(random() * 50000))::bigint,
  (ARRAY[
    'shipping my side project today',
    'coffee first, then code',
    'anyone else benchmarking stuff?',
    'hello world',
    'just refactored my whole life',
    'deployed on a friday, wish me luck',
    'this one weird sql trick changed everything',
    'p99 is a lifestyle not a metric',
    'reading the docs actually works',
    'my cache is now 100% hits (n=1)',
    'cargo build --release and pray',
    ' why is prod always out of memory',
    'the answer was a missing index. it is always a missing index',
    'tail latency haunts my dreams',
    'garbage collected my weekend',
    'green threads, red eyes',
    'compiled languages compile my confidence',
    'orm said no',
    'raw sql said yes',
    'another day another rollout',
    'premature optimization is my cardio',
    '1 core is all you need (probably)',
    'load test went brrr',
    'backlog zero. motivation zero.'
  ])[1 + floor(random() * 24)],
  now() - (random() * interval '365 days')
FROM generate_series(1, 500000) AS g;

INSERT INTO likes (post_id, user_id, created_at)
SELECT
  (1 + floor(random() * 500000))::bigint,
  (1 + floor(random() * 50000))::bigint,
  now() - (random() * interval '365 days')
FROM generate_series(1, 2200000) AS g
ON CONFLICT (post_id, user_id) DO NOTHING;

ANALYZE users;
ANALYZE posts;
ANALYZE likes;
