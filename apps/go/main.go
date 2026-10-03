package main

import (
	"context"
	"errors"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgconn"
	"github.com/jackc/pgx/v5/pgxpool"
	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/promauto"
	"github.com/prometheus/client_golang/prometheus/promhttp"
)

// Canonical SQL — MUST match sql/queries.sql exactly (Q1, Q2, Q3, Q4a-c).
const (
	// Q1 (GET /feed?page=N) — CTE form: pick 20 posts via index BEFORE joining likes
	qFeed = `WITH feed AS (
	      SELECT p.id, p.user_id, p.content, p.created_at
	      FROM posts p
	      ORDER BY p.created_at DESC, p.id DESC
	      LIMIT 20 OFFSET ($1 - 1) * 20
	)
	SELECT f.id, f.user_id, u.username, f.content, f.created_at,
	       COUNT(l.id)::bigint AS like_count
	FROM feed f
	JOIN users u ON u.id = f.user_id
	LEFT JOIN likes l ON l.post_id = f.id
	GROUP BY f.id, f.user_id, u.username, f.content, f.created_at
	ORDER BY f.created_at DESC, f.id DESC`

	// Q2 (GET /posts/:id)
	qPost = `SELECT p.id, p.user_id, u.username, p.content, p.created_at,
	       COUNT(l.id)::bigint AS like_count
	FROM posts p
	JOIN users u ON u.id = p.user_id
	LEFT JOIN likes l ON l.post_id = p.id
	WHERE p.id = $1
	GROUP BY p.id, p.user_id, u.username, p.content, p.created_at`

	// Q3 (POST /posts) — single statement, like_count computed in the app.
	qCreatePost = `INSERT INTO posts (user_id, content, created_at)
	VALUES ($1, $2, now())
	RETURNING id, user_id, content, created_at`

	// Q4a (POST /posts/:id/like) — post existence check.
	qLikeExists = `SELECT 1 FROM posts WHERE id = $1`

	// Q4b — idempotent insert.
	qLikeInsert = `INSERT INTO likes (post_id, user_id, created_at)
	VALUES ($1, $2, now())
	ON CONFLICT (post_id, user_id) DO NOTHING`

	// Q4c — fresh count for the response.
	qLikeCount = `SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1`
)

var (
	httpDuration = promauto.NewHistogramVec(prometheus.HistogramOpts{
		Name:    "http_request_duration_seconds",
		Help:    "HTTP request latency in seconds.",
		Buckets: []float64{0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10},
	}, []string{"method", "route", "status"})

	httpRequests = promauto.NewCounterVec(prometheus.CounterOpts{
		Name: "http_requests_total",
		Help: "Total number of HTTP requests.",
	}, []string{"method", "route", "status"})

	appMemoryRSS = promauto.NewGauge(prometheus.GaugeOpts{
		Name: "app_memory_rss_bytes",
		Help: "Process resident set size (VmRSS) in bytes.",
	})
)

type postItem struct {
	ID        int64     `json:"id"`
	UserID    int64     `json:"user_id"`
	Username  string    `json:"username"`
	Content   string    `json:"content"`
	LikeCount int64     `json:"like_count"`
	CreatedAt time.Time `json:"created_at"`
}

type createdPost struct {
	ID        int64     `json:"id"`
	UserID    int64     `json:"user_id"`
	Content   string    `json:"content"`
	CreatedAt time.Time `json:"created_at"`
	LikeCount int64     `json:"like_count"`
}

func main() {
	dbURL := os.Getenv("DATABASE_URL")
	if dbURL == "" {
		log.Fatal("DATABASE_URL is required")
	}

	cfg, err := pgxpool.ParseConfig(dbURL)
	if err != nil {
		log.Fatalf("parse DATABASE_URL: %v", err)
	}
	// Pool cap: POOL_SIZE env var, default 8 (per SPEC); non-numeric → 8.
	cfg.MaxConns = 8
	if v := os.Getenv("POOL_SIZE"); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			cfg.MaxConns = int32(n)
		}
	}

	pool, err := pgxpool.NewWithConfig(context.Background(), cfg)
	if err != nil {
		log.Fatalf("create pool: %v", err)
	}
	defer pool.Close()

	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	// metrics middleware outermost so panics (handled by Recovery) are still recorded
	r.Use(requestMetrics())
	r.Use(gin.Recovery())

	r.GET("/feed", feedHandler(pool))
	r.GET("/posts/:id", getPostHandler(pool))
	r.POST("/posts", createPostHandler(pool))
	r.POST("/posts/:id/like", likePostHandler(pool))
	r.GET("/healthz", healthHandler(pool))
	r.GET("/metrics", gin.WrapH(promhttp.Handler()))

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	log.Printf("listening on :%s", port)
	if err := r.Run(":" + port); err != nil {
		log.Fatal(err)
	}
}

// requestMetrics observes duration/counter and updates the RSS gauge for every
// recorded request. /metrics and /healthz are never recorded.
func requestMetrics() gin.HandlerFunc {
	return func(c *gin.Context) {
		path := c.Request.URL.Path
		if path == "/metrics" || path == "/healthz" {
			c.Next()
			return
		}

		start := time.Now()
		c.Next()

		route := c.FullPath()
		status := strconv.Itoa(c.Writer.Status())
		httpDuration.WithLabelValues(c.Request.Method, route, status).Observe(time.Since(start).Seconds())
		httpRequests.WithLabelValues(c.Request.Method, route, status).Inc()
		appMemoryRSS.Set(readRSSBytes())
	}
}

// readRSSBytes parses VmRSS (kB) from /proc/self/status and returns bytes.
func readRSSBytes() float64 {
	data, err := os.ReadFile("/proc/self/status")
	if err != nil {
		return 0
	}
	for _, line := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(line, "VmRSS:") {
			continue
		}
		fields := strings.Fields(line)
		if len(fields) < 2 {
			return 0
		}
		kb, err := strconv.ParseFloat(fields[1], 64)
		if err != nil {
			return 0
		}
		return kb * 1024
	}
	return 0
}

func feedHandler(pool *pgxpool.Pool) gin.HandlerFunc {
	return func(c *gin.Context) {
		page, err := strconv.Atoi(c.Query("page"))
		if err != nil || page < 1 {
			page = 1
		}

		rows, err := pool.Query(c.Request.Context(), qFeed, int32(page))
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		defer rows.Close()

		posts := make([]postItem, 0, 20)
		for rows.Next() {
			var p postItem
			var createdAt time.Time
			if err := rows.Scan(&p.ID, &p.UserID, &p.Username, &p.Content, &createdAt, &p.LikeCount); err != nil {
				c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
				return
			}
			p.CreatedAt = createdAt.UTC()
			posts = append(posts, p)
		}
		if err := rows.Err(); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		c.JSON(http.StatusOK, gin.H{"page": page, "posts": posts})
	}
}

func getPostHandler(pool *pgxpool.Pool) gin.HandlerFunc {
	return func(c *gin.Context) {
		id, err := strconv.ParseInt(c.Param("id"), 10, 64)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid post id"})
			return
		}

		var p postItem
		var createdAt time.Time
		err = pool.QueryRow(c.Request.Context(), qPost, id).
			Scan(&p.ID, &p.UserID, &p.Username, &p.Content, &createdAt, &p.LikeCount)
		if errors.Is(err, pgx.ErrNoRows) {
			c.JSON(http.StatusNotFound, gin.H{"error": "post not found"})
			return
		}
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}
		p.CreatedAt = createdAt.UTC()

		c.JSON(http.StatusOK, p)
	}
}

func createPostHandler(pool *pgxpool.Pool) gin.HandlerFunc {
	return func(c *gin.Context) {
		var req struct {
			UserID  int64  `json:"user_id"`
			Content string `json:"content"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}

		var (
			id        int64
			userID    int64
			content   string
			createdAt time.Time
		)
		err := pool.QueryRow(c.Request.Context(), qCreatePost, req.UserID, req.Content).
			Scan(&id, &userID, &content, &createdAt)
		if err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23503" { // SQLSTATE foreign_key_violation
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid user_id"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		c.JSON(http.StatusCreated, createdPost{
			ID:        id,
			UserID:    userID,
			Content:   content,
			CreatedAt: createdAt.UTC(),
			LikeCount: 0,
		})
	}
}

func likePostHandler(pool *pgxpool.Pool) gin.HandlerFunc {
	return func(c *gin.Context) {
		id, err := strconv.ParseInt(c.Param("id"), 10, 64)
		if err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid post id"})
			return
		}

		var req struct {
			UserID int64 `json:"user_id"`
		}
		if err := c.ShouldBindJSON(&req); err != nil {
			c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
			return
		}

		ctx := c.Request.Context()

		// Q4a
		var exists int
		err = pool.QueryRow(ctx, qLikeExists, id).Scan(&exists)
		if errors.Is(err, pgx.ErrNoRows) {
			c.JSON(http.StatusNotFound, gin.H{"error": "post not found"})
			return
		}
		if err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// Q4b
		if _, err := pool.Exec(ctx, qLikeInsert, id, req.UserID); err != nil {
			var pgErr *pgconn.PgError
			if errors.As(err, &pgErr) && pgErr.Code == "23503" { // SQLSTATE foreign_key_violation
				c.JSON(http.StatusBadRequest, gin.H{"error": "invalid user_id"})
				return
			}
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		// Q4c
		var likeCount int64
		if err := pool.QueryRow(ctx, qLikeCount, id).Scan(&likeCount); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "internal server error"})
			return
		}

		c.JSON(http.StatusOK, gin.H{"post_id": id, "like_count": likeCount})
	}
}

func healthHandler(pool *pgxpool.Pool) gin.HandlerFunc {
	return func(c *gin.Context) {
		if _, err := pool.Exec(c.Request.Context(), "SELECT 1"); err != nil {
			c.JSON(http.StatusServiceUnavailable, gin.H{"status": "error"})
			return
		}
		c.JSON(http.StatusOK, gin.H{"status": "ok"})
	}
}
