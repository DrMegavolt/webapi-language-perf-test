using System.Diagnostics;
using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;
using Npgsql;
using Prometheus;

var builder = WebApplication.CreateBuilder(args);

// Listen on 0.0.0.0:8080, honoring the PORT env var.
var port = Environment.GetEnvironmentVariable("PORT") ?? "8080";
builder.WebHost.UseUrls($"http://0.0.0.0:{port}");

var databaseUrl = Environment.GetEnvironmentVariable("DATABASE_URL")
    ?? throw new InvalidOperationException("DATABASE_URL environment variable is required");

// The spec's DATABASE_URL is a postgres:// URI; Npgsql 10's builder wants key/value
// form, so parse the URI here (pool capped at 8 connections per the spec).
var dataSource = NpgsqlDataSource.Create(BuildConnectionString(databaseUrl));

// Hand-rolled metrics on the default registry (exact names, labels and buckets per the spec).
var durationHistogram = Metrics.CreateHistogram(
    "http_request_duration_seconds",
    "Duration of HTTP requests in seconds.",
    new HistogramConfiguration
    {
        LabelNames = new[] { "method", "route", "status" },
        Buckets = new double[] { 0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10 },
    });

var requestCounter = Metrics.CreateCounter(
    "http_requests_total",
    "Total number of HTTP requests.",
    new CounterConfiguration { LabelNames = new[] { "method", "route", "status" } });

var memoryGauge = Metrics.CreateGauge(
    "app_memory_rss_bytes",
    "Resident memory of the process in bytes (VmRSS from /proc/self/status).");

var postsLikeRegex = new Regex("^/posts/\\d+/like$", RegexOptions.Compiled);
var singlePostRegex = new Regex("^/posts/\\d+$", RegexOptions.Compiled);

string? MapRouteLabel(string? path)
{
    if (string.IsNullOrEmpty(path))
    {
        return null;
    }

    if (path is "/feed" or "/posts")
    {
        return path;
    }

    if (postsLikeRegex.IsMatch(path))
    {
        return "/posts/:id/like";
    }

    if (singlePostRegex.IsMatch(path))
    {
        return "/posts/:id";
    }

    // Unknown routes (including /metrics and /healthz) are not recorded.
    return null;
}

var app = builder.Build();

// Registered before the endpoint mappings so it wraps the full request handling.
app.Use(async (context, next) =>
{
    var route = MapRouteLabel(context.Request.Path.Value);
    if (route is null)
    {
        await next(context);
        return;
    }

    var start = Stopwatch.GetTimestamp();
    try
    {
        await next(context);
    }
    finally
    {
        var elapsed = Stopwatch.GetElapsedTime(start).TotalSeconds;
        var method = context.Request.Method;
        var status = context.Response.StatusCode.ToString(CultureInfo.InvariantCulture);
        durationHistogram.WithLabels(method, route, status).Observe(elapsed);
        requestCounter.WithLabels(method, route, status).Inc();

        var rssBytes = ReadRssBytes();
        if (rssBytes is not null)
        {
            memoryGauge.Set(rssBytes.Value);
        }
    }
});

// Q1 (GET /feed?page=N) — canonical statement from sql/queries.sql, verbatim.
const string FeedSql = """
    SELECT p.id, p.user_id, u.username, p.content, p.created_at,
           COUNT(l.id)::bigint AS like_count
    FROM posts p
    JOIN users u ON u.id = p.user_id
    LEFT JOIN likes l ON l.post_id = p.id
    GROUP BY p.id, p.user_id, u.username, p.content, p.created_at
    ORDER BY p.created_at DESC, p.id DESC
    LIMIT 20 OFFSET ($1 - 1) * 20;
    """;

app.MapGet("/feed", async Task<IResult> (HttpContext context) =>
{
    var rawPage = context.Request.Query["page"].ToString();
    var page = int.TryParse(rawPage, NumberStyles.Integer, CultureInfo.InvariantCulture, out var parsed) && parsed >= 1
        ? parsed
        : 1;

    var posts = new List<object>(20);
    await using var conn = await dataSource.OpenConnectionAsync();
    await using (var cmd = new NpgsqlCommand(FeedSql, conn))
    {
        cmd.Parameters.AddWithValue(page);
        await using var reader = await cmd.ExecuteReaderAsync();
        while (await reader.ReadAsync())
        {
            posts.Add(new
            {
                id = reader.GetInt64(0),
                user_id = reader.GetInt64(1),
                username = reader.GetString(2),
                content = reader.GetString(3),
                created_at = reader.GetDateTime(4),
                like_count = reader.GetInt64(5),
            });
        }
    }

    return Results.Ok(new { page, posts });
});

// Q3 (POST /posts) — canonical statement from sql/queries.sql, verbatim.
// Exactly one statement: no user pre-check, no join (a missing user_id violates
// the FK and is mapped to 400 below).
const string CreatePostSql = """
    INSERT INTO posts (user_id, content, created_at)
    VALUES ($1, $2, now())
    RETURNING id, user_id, content, created_at;
    """;

app.MapPost("/posts", async Task<IResult> (HttpContext context) =>
{
    JsonDocument doc;
    try
    {
        doc = await JsonDocument.ParseAsync(context.Request.Body);
    }
    catch (JsonException)
    {
        return Results.BadRequest(new { error = "invalid body" });
    }

    using (doc)
    {
        var root = doc.RootElement;
        if (!root.TryGetProperty("user_id", out var userIdElement)
            || userIdElement.ValueKind != JsonValueKind.Number
            || !userIdElement.TryGetInt64(out var userId))
        {
            return Results.BadRequest(new { error = "invalid user_id" });
        }

        if (!root.TryGetProperty("content", out var contentElement)
            || contentElement.ValueKind != JsonValueKind.String)
        {
            return Results.BadRequest(new { error = "invalid content" });
        }

        var content = contentElement.GetString()!;

        await using var conn = await dataSource.OpenConnectionAsync();
        long id;
        DateTime createdAt;
        await using (var cmd = new NpgsqlCommand(CreatePostSql, conn))
        {
            cmd.Parameters.AddWithValue(userId);
            cmd.Parameters.AddWithValue(content);
            try
            {
                await using var reader = await cmd.ExecuteReaderAsync();
                await reader.ReadAsync();
                id = reader.GetInt64(0);
                createdAt = reader.GetDateTime(3);
            }
            catch (PostgresException ex) when (ex.SqlState == "23503")
            {
                // FK violation: nonexistent user_id -> 400.
                return Results.BadRequest(new { error = "invalid user_id" });
            }
        }

        return Results.Json(
            new { id, user_id = userId, content, created_at = createdAt, like_count = 0L },
            statusCode: 201);
    }
});

// Q4a-c (POST /posts/:id/like) — canonical statements from sql/queries.sql, verbatim.
const string PostExistsSql = "SELECT 1 FROM posts WHERE id = $1;";
const string InsertLikeSql = """
    INSERT INTO likes (post_id, user_id, created_at)
    VALUES ($1, $2, now())
    ON CONFLICT (post_id, user_id) DO NOTHING;
    """;
const string LikeCountSql = "SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1;";

app.MapPost("/posts/{id}/like", async Task<IResult> (HttpContext context, string id) =>
{
    if (!long.TryParse(id, NumberStyles.Integer, CultureInfo.InvariantCulture, out var postId))
    {
        return Results.BadRequest(new { error = "invalid post id" });
    }

    JsonDocument doc;
    try
    {
        doc = await JsonDocument.ParseAsync(context.Request.Body);
    }
    catch (JsonException)
    {
        return Results.BadRequest(new { error = "invalid body" });
    }

    using (doc)
    {
        var root = doc.RootElement;
        if (!root.TryGetProperty("user_id", out var userIdElement)
            || userIdElement.ValueKind != JsonValueKind.Number
            || !userIdElement.TryGetInt64(out var userId))
        {
            return Results.BadRequest(new { error = "invalid user_id" });
        }

        await using var conn = await dataSource.OpenConnectionAsync();

        // 4a: missing post -> 404.
        await using (var cmd = new NpgsqlCommand(PostExistsSql, conn))
        {
            cmd.Parameters.AddWithValue(postId);
            if (await cmd.ExecuteScalarAsync() is null)
            {
                return Results.NotFound(new { error = "post not found" });
            }
        }

        // 4b: idempotent insert.
        try
        {
            await using (var cmd = new NpgsqlCommand(InsertLikeSql, conn))
            {
                cmd.Parameters.AddWithValue(postId);
                cmd.Parameters.AddWithValue(userId);
                await cmd.ExecuteNonQueryAsync();
            }
        }
        catch (PostgresException ex) when (ex.SqlState == "23503")
        {
            return Results.BadRequest(new { error = "invalid user_id" });
        }

        // 4c: fresh count for the response.
        long likeCount;
        await using (var cmd = new NpgsqlCommand(LikeCountSql, conn))
        {
            cmd.Parameters.AddWithValue(postId);
            likeCount = (long)(await cmd.ExecuteScalarAsync())!;
        }

        return Results.Ok(new { post_id = postId, like_count = likeCount });
    }
});

// Q2 (GET /posts/:id) — canonical statement from sql/queries.sql, verbatim.
const string SinglePostSql = """
    SELECT p.id, p.user_id, u.username, p.content, p.created_at,
           COUNT(l.id)::bigint AS like_count
    FROM posts p
    JOIN users u ON u.id = p.user_id
    LEFT JOIN likes l ON l.post_id = p.id
    WHERE p.id = $1
    GROUP BY p.id, p.user_id, u.username, p.content, p.created_at;
    """;

app.MapGet("/posts/{id}", async Task<IResult> (string id) =>
{
    if (!long.TryParse(id, NumberStyles.Integer, CultureInfo.InvariantCulture, out var postId))
    {
        return Results.BadRequest(new { error = "invalid post id" });
    }

    await using var conn = await dataSource.OpenConnectionAsync();
    await using (var cmd = new NpgsqlCommand(SinglePostSql, conn))
    {
        cmd.Parameters.AddWithValue(postId);
        await using var reader = await cmd.ExecuteReaderAsync();
        if (!await reader.ReadAsync())
        {
            return Results.NotFound(new { error = "post not found" });
        }

        return Results.Ok(new
        {
            id = reader.GetInt64(0),
            user_id = reader.GetInt64(1),
            username = reader.GetString(2),
            content = reader.GetString(3),
            created_at = reader.GetDateTime(4),
            like_count = reader.GetInt64(5),
        });
    }
});

app.MapGet("/healthz", async Task<IResult> () =>
{
    await using var conn = await dataSource.OpenConnectionAsync();
    await using var cmd = new NpgsqlCommand("SELECT 1", conn);
    await cmd.ExecuteScalarAsync();
    return Results.Ok(new { status = "ok" });
});

app.MapMetrics("/metrics");

app.Run();

static string BuildConnectionString(string url)
{
    var uri = new Uri(url);
    var builder = new NpgsqlConnectionStringBuilder
    {
        Host = uri.Host,
        Port = uri.Port > 0 ? uri.Port : 5432,
        Database = uri.AbsolutePath.TrimStart('/'),
        MaxPoolSize = 8,
    };

    var userInfo = uri.UserInfo.Split(':', 2);
    if (userInfo[0].Length > 0)
    {
        builder.Username = Uri.UnescapeDataString(userInfo[0]);
    }

    if (userInfo.Length > 1)
    {
        builder.Password = Uri.UnescapeDataString(userInfo[1]);
    }

    var query = uri.Query.TrimStart('?');
    foreach (var pair in query.Split('&', StringSplitOptions.RemoveEmptyEntries))
    {
        var keyAndValue = pair.Split('=', 2);
        builder[Uri.UnescapeDataString(keyAndValue[0])] =
            Uri.UnescapeDataString(keyAndValue.Length > 1 ? keyAndValue[1] : string.Empty);
    }

    return builder.ConnectionString;
}

static long? ReadRssBytes()
{
    try
    {
        foreach (var line in File.ReadLines("/proc/self/status"))
        {
            if (line.StartsWith("VmRSS:", StringComparison.Ordinal))
            {
                var fields = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (fields.Length >= 2
                    && long.TryParse(fields[1], NumberStyles.Integer, CultureInfo.InvariantCulture, out var kilobytes))
                {
                    return kilobytes * 1024;
                }

                return null;
            }
        }
    }
    catch (IOException)
    {
        // /proc not available (non-Linux); leave the gauge at its last value.
    }

    return null;
}
