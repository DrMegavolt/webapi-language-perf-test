# frozen_string_literal: true

class PostsController < ApplicationController
  # GET /posts/:id (canonical Q2) — single post with author + like count.
  def show
    id = parse_integer(params[:id])
    return render_error(:bad_request, "invalid post id") if id.nil?

    row = LangperfOtel.db_span("DB Q2 single post") do
      ActiveRecord::Base.connection.exec_query(
        <<~SQL.squish, "posts.show", [sql_bind("id", id, INT_TYPE)]
          SELECT p.id, p.user_id, u.username, p.content, p.created_at,
                 COUNT(l.id)::bigint AS like_count
          FROM posts p
          JOIN users u ON u.id = p.user_id
          LEFT JOIN likes l ON l.post_id = p.id
          WHERE p.id = $1
          GROUP BY p.id, p.user_id, u.username, p.content, p.created_at
        SQL
      ).first
    end

    return render_error(:not_found, "post not found") unless row

    render json: post_json(row)
  end

  # POST /posts {"user_id": 123, "content": "..."} (canonical Q3) — exactly one
  # statement. A missing user violates the FK; that driver error maps to 400.
  def create
    user_id = parse_integer(params[:user_id])
    return render_error(:bad_request, "invalid user_id") if user_id.nil?

    content = params[:content]
    return render_error(:bad_request, "invalid content") unless content.is_a?(String)

    row = LangperfOtel.db_span("DB Q3 create post") do
      ActiveRecord::Base.connection.exec_query(
        <<~SQL.squish, "posts.create", [sql_bind("user_id", user_id, INT_TYPE), sql_bind("content", content, TEXT_TYPE)]
          INSERT INTO posts (user_id, content, created_at)
          VALUES ($1, $2, now())
          RETURNING id, user_id, content, created_at
        SQL
      ).first
    end

    render json: {
      "id" => row["id"],
      "user_id" => row["user_id"],
      "content" => row["content"],
      "created_at" => iso8601_utc(row["created_at"]),
      "like_count" => 0 # computed in the app, never queried
    }, status: :created
  rescue ActiveRecord::InvalidForeignKey
    render_error(:bad_request, "invalid user_id")
  end

  private

  # Feed item shape: {"id","user_id","username","content","like_count","created_at"}.
  def post_json(row)
    {
      "id" => row["id"],
      "user_id" => row["user_id"],
      "username" => row["username"],
      "content" => row["content"],
      "like_count" => row["like_count"],
      "created_at" => iso8601_utc(row["created_at"])
    }
  end
end
