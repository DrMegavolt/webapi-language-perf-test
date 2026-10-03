use std::str::FromStr;
use std::sync::LazyLock;
use std::time::Instant;

use actix_web::body::MessageBody;
use actix_web::dev::{ServiceRequest, ServiceResponse};
use actix_web::http::StatusCode;
use actix_web::middleware::{self, Next};
use actix_web::{web, App, Error, HttpResponse, HttpServer};
use chrono::{DateTime, Utc};
use deadpool_postgres::{Manager, ManagerConfig, Pool, RecyclingMethod};
use prometheus::{
    default_registry, Encoder, HistogramOpts, HistogramVec, IntCounterVec, IntGauge, Opts,
    TextEncoder,
};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio_postgres::error::SqlState;
use tokio_postgres::{Config, NoTls};

// ---------------------------------------------------------------------------
// Metrics — names, labels and buckets are fixed by SPEC.md
// ---------------------------------------------------------------------------

const HTTP_BUCKETS: &[f64] = &[
    0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0,
];

static HTTP_DURATION: LazyLock<HistogramVec> = LazyLock::new(|| {
    let h = HistogramVec::new(
        HistogramOpts::new(
            "http_request_duration_seconds",
            "Duration of HTTP requests in seconds.",
        )
        .buckets(HTTP_BUCKETS.to_vec()),
        &["method", "route", "status"],
    )
    .expect("valid histogram vec");
    default_registry()
        .register(Box::new(h.clone()))
        .expect("register http_request_duration_seconds");
    h
});

static HTTP_REQUESTS_TOTAL: LazyLock<IntCounterVec> = LazyLock::new(|| {
    let c = IntCounterVec::new(
        Opts::new("http_requests_total", "Total number of HTTP requests."),
        &["method", "route", "status"],
    )
    .expect("valid counter vec");
    default_registry()
        .register(Box::new(c.clone()))
        .expect("register http_requests_total");
    c
});

static APP_MEMORY_RSS_BYTES: LazyLock<IntGauge> = LazyLock::new(|| {
    let g = IntGauge::new(
        "app_memory_rss_bytes",
        "Process resident memory in bytes (VmRSS from /proc/self/status).",
    )
    .expect("valid gauge");
    default_registry()
        .register(Box::new(g.clone()))
        .expect("register app_memory_rss_bytes");
    g
});

fn read_rss_bytes() -> i64 {
    let Ok(status) = std::fs::read_to_string("/proc/self/status") else {
        return 0;
    };
    for line in status.lines() {
        if let Some(rest) = line.strip_prefix("VmRSS:") {
            if let Some(kb) = rest
                .split_whitespace()
                .next()
                .and_then(|s| s.parse::<i64>().ok())
            {
                return kb * 1024;
            }
        }
    }
    0
}

/// Map the matched route pattern + method to the SPEC route label constants.
/// Returns None for /metrics, /healthz and unmatched routes (never recorded).
fn route_label(method: &str, pattern: Option<&str>) -> Option<&'static str> {
    match (method, pattern) {
        ("GET", Some("/feed")) => Some("/feed"),
        ("POST", Some("/posts")) => Some("/posts"),
        ("GET", Some("/posts/{id}")) => Some("/posts/:id"),
        ("POST", Some("/posts/{id}/like")) => Some("/posts/:id/like"),
        _ => None,
    }
}

async fn metrics_mw(
    req: ServiceRequest,
    next: Next<impl MessageBody>,
) -> Result<ServiceResponse<impl MessageBody>, Error> {
    let route = route_label(req.method().as_str(), req.match_pattern().as_deref());
    let method = req.method().as_str().to_owned();
    let start = Instant::now();
    let res = next.call(req).await?;
    if let Some(route) = route {
        let code = res.status();
        let status = code.as_str();
        HTTP_DURATION
            .with_label_values(&[&method, route, status])
            .observe(start.elapsed().as_secs_f64());
        HTTP_REQUESTS_TOTAL
            .with_label_values(&[&method, route, status])
            .inc();
        APP_MEMORY_RSS_BYTES.set(read_rss_bytes());
    }
    Ok(res)
}

// ---------------------------------------------------------------------------
// Canonical SQL — must match sql/queries.sql exactly (Q1, Q2, Q3, Q4a-c)
// ---------------------------------------------------------------------------

const Q_FEED: &str = "SELECT p.id, p.user_id, u.username, p.content, p.created_at, \
                            COUNT(l.id)::bigint AS like_count \
                     FROM posts p \
                     JOIN users u ON u.id = p.user_id \
                     LEFT JOIN likes l ON l.post_id = p.id \
                     GROUP BY p.id, p.user_id, u.username, p.content, p.created_at \
                     ORDER BY p.created_at DESC, p.id DESC \
                     LIMIT 20 OFFSET ($1 - 1) * 20";

const Q_POST: &str = "SELECT p.id, p.user_id, u.username, p.content, p.created_at, \
                            COUNT(l.id)::bigint AS like_count \
                      FROM posts p \
                      JOIN users u ON u.id = p.user_id \
                      LEFT JOIN likes l ON l.post_id = p.id \
                      WHERE p.id = $1 \
                      GROUP BY p.id, p.user_id, u.username, p.content, p.created_at";

const Q_CREATE_POST: &str = "INSERT INTO posts (user_id, content, created_at) \
                             VALUES ($1, $2, now()) \
                             RETURNING id, user_id, content, created_at";

const Q_LIKE_CHECK: &str = "SELECT 1 FROM posts WHERE id = $1";

const Q_LIKE_INSERT: &str = "INSERT INTO likes (post_id, user_id, created_at) \
                             VALUES ($1, $2, now()) \
                             ON CONFLICT (post_id, user_id) DO NOTHING";

const Q_LIKE_COUNT: &str =
    "SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1";

// ---------------------------------------------------------------------------
// Payload / response types
// ---------------------------------------------------------------------------

#[derive(Deserialize)]
struct FeedQuery {
    // i32 (int4): postgres infers $1 in Q1's `OFFSET ($1 - 1) * 20` as integer,
    // so the parameter must bind as int4 to keep the canonical statement text.
    page: Option<i32>,
}

// Feed / single-post item shape: id, user_id, username, content, like_count, created_at
#[derive(Serialize)]
struct PostItem {
    id: i64,
    user_id: i64,
    username: String,
    content: String,
    like_count: i64,
    created_at: DateTime<Utc>,
}

#[derive(Serialize)]
struct FeedResponse {
    page: i32,
    posts: Vec<PostItem>,
}

#[derive(Deserialize)]
struct CreatePostBody {
    user_id: i64,
    content: String,
}

// Create-post response shape: id, user_id, content, created_at, like_count (no username)
#[derive(Serialize)]
struct CreatedPost {
    id: i64,
    user_id: i64,
    content: String,
    created_at: DateTime<Utc>,
    like_count: i64,
}

#[derive(Deserialize)]
struct LikeBody {
    user_id: i64,
}

#[derive(Serialize)]
struct LikeResponse {
    post_id: i64,
    like_count: i64,
}

fn err_json(status: StatusCode, msg: &str) -> HttpResponse {
    HttpResponse::build(status).json(json!({ "error": msg }))
}

async fn db_client(pool: &Pool) -> Result<deadpool_postgres::Client, HttpResponse> {
    pool.get().await.map_err(|_| {
        err_json(StatusCode::SERVICE_UNAVAILABLE, "db unavailable")
    })
}

// ---------------------------------------------------------------------------
// Handlers
// ---------------------------------------------------------------------------

async fn feed(pool: web::Data<Pool>, q: web::Query<FeedQuery>) -> HttpResponse {
    let page = q.page.unwrap_or(1).max(1);
    let client = match db_client(&pool).await {
        Ok(c) => c,
        Err(res) => return res,
    };
    match client.query(Q_FEED, &[&page]).await {
        Ok(rows) => {
            let posts = rows
                .iter()
                .map(|r| PostItem {
                    id: r.get(0),
                    user_id: r.get(1),
                    username: r.get(2),
                    content: r.get(3),
                    like_count: r.get(5),
                    created_at: r.get(4),
                })
                .collect();
            HttpResponse::Ok().json(FeedResponse { page, posts })
        }
        Err(_) => err_json(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    }
}

async fn get_post(pool: web::Data<Pool>, path: web::Path<String>) -> HttpResponse {
    let Ok(id) = path.parse::<i64>() else {
        return err_json(StatusCode::BAD_REQUEST, "invalid post id");
    };
    let client = match db_client(&pool).await {
        Ok(c) => c,
        Err(res) => return res,
    };
    match client.query_opt(Q_POST, &[&id]).await {
        Ok(Some(r)) => HttpResponse::Ok().json(PostItem {
            id: r.get(0),
            user_id: r.get(1),
            username: r.get(2),
            content: r.get(3),
            like_count: r.get(5),
            created_at: r.get(4),
        }),
        Ok(None) => err_json(StatusCode::NOT_FOUND, "post not found"),
        Err(_) => err_json(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    }
}

async fn create_post(pool: web::Data<Pool>, body: web::Json<CreatePostBody>) -> HttpResponse {
    let client = match db_client(&pool).await {
        Ok(c) => c,
        Err(res) => return res,
    };
    match client
        .query_one(Q_CREATE_POST, &[&body.user_id, &body.content])
        .await
    {
        Ok(r) => HttpResponse::Created().json(CreatedPost {
            id: r.get(0),
            user_id: r.get(1),
            content: r.get(2),
            created_at: r.get(3),
            like_count: 0,
        }),
        Err(e) if e.code() == Some(&SqlState::FOREIGN_KEY_VIOLATION) => {
            err_json(StatusCode::BAD_REQUEST, "invalid user_id")
        }
        Err(_) => err_json(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    }
}

async fn like_post(
    pool: web::Data<Pool>,
    path: web::Path<String>,
    body: web::Json<LikeBody>,
) -> HttpResponse {
    let Ok(post_id) = path.parse::<i64>() else {
        return err_json(StatusCode::BAD_REQUEST, "invalid post id");
    };
    let client = match db_client(&pool).await {
        Ok(c) => c,
        Err(res) => return res,
    };
    // Q4a: post existence check — no row -> 404
    match client.query_opt(Q_LIKE_CHECK, &[&post_id]).await {
        Ok(Some(_)) => {}
        Ok(None) => return err_json(StatusCode::NOT_FOUND, "post not found"),
        Err(_) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    }
    // Q4b: idempotent insert
    if client
        .execute(Q_LIKE_INSERT, &[&post_id, &body.user_id])
        .await
        .is_err()
    {
        return err_json(StatusCode::INTERNAL_SERVER_ERROR, "internal error");
    }
    // Q4c: fresh count for the response
    match client.query_one(Q_LIKE_COUNT, &[&post_id]).await {
        Ok(r) => HttpResponse::Ok().json(LikeResponse {
            post_id,
            like_count: r.get(0),
        }),
        Err(_) => err_json(StatusCode::INTERNAL_SERVER_ERROR, "internal error"),
    }
}

async fn healthz(pool: web::Data<Pool>) -> HttpResponse {
    let client = match db_client(&pool).await {
        Ok(c) => c,
        Err(res) => return res,
    };
    match client.simple_query("SELECT 1").await {
        Ok(_) => HttpResponse::Ok().json(json!({ "status": "ok" })),
        Err(_) => err_json(StatusCode::SERVICE_UNAVAILABLE, "unhealthy"),
    }
}

async fn metrics_handler() -> HttpResponse {
    let encoder = TextEncoder::new();
    let mut buf = Vec::new();
    let _ = encoder.encode(&default_registry().gather(), &mut buf);
    match String::from_utf8(buf) {
        Ok(body) => HttpResponse::Ok()
            .content_type(encoder.format_type())
            .body(body),
        Err(_) => err_json(StatusCode::INTERNAL_SERVER_ERROR, "metrics encode error"),
    }
}

// ---------------------------------------------------------------------------
// Bootstrap
// ---------------------------------------------------------------------------

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    let port: u16 = std::env::var("PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080);
    let db_url = std::env::var("DATABASE_URL").expect("DATABASE_URL must be set");
    let pg_config = Config::from_str(&db_url).expect("invalid DATABASE_URL");
    let manager = Manager::from_config(
        pg_config,
        NoTls,
        ManagerConfig {
            recycling_method: RecyclingMethod::Fast,
        },
    );
    let pool = Pool::builder(manager)
        .max_size(8)
        .build()
        .expect("failed to build connection pool");

    // Force metric registration before serving traffic.
    LazyLock::force(&HTTP_DURATION);
    LazyLock::force(&HTTP_REQUESTS_TOTAL);
    LazyLock::force(&APP_MEMORY_RSS_BYTES);

    println!("listening on 0.0.0.0:{port}");

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(pool.clone()))
            .wrap(middleware::from_fn(metrics_mw))
            .route("/feed", web::get().to(feed))
            .route("/posts", web::post().to(create_post))
            .route("/posts/{id}", web::get().to(get_post))
            .route("/posts/{id}/like", web::post().to(like_post))
            .route("/healthz", web::get().to(healthz))
            .route("/metrics", web::get().to(metrics_handler))
    })
    .workers(1) // SPEC: actix must use ONE core
    .bind(("0.0.0.0", port))?
    .run()
    .await
}
