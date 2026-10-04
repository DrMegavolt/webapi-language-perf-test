# frozen_string_literal: true

class LikesController < ApplicationController
  # POST /posts/:id/like {"user_id": 456} — idempotent per (post_id, user_id).
  def create
    post_id = parse_integer(params[:id])
    return render_error(:bad_request, "invalid post id") if post_id.nil?

    user_id = parse_integer(params[:user_id])
    return render_error(:bad_request, "invalid user_id") if user_id.nil? || user_id < 1

    conn = ActiveRecord::Base.connection
    exists = LangperfOtel.db_span("DB Q4a post exists") do
      conn.select_value(
        "SELECT 1 FROM posts WHERE id = $1",
        "likes.post_exists",
        [sql_bind("id", post_id, INT_TYPE)]
      )
    end
    return render_error(:not_found, "post not found") if exists.nil?

    LangperfOtel.db_span("DB Q4b insert like") do
      conn.exec_query(
        "INSERT INTO likes (post_id, user_id, created_at) VALUES ($1, $2, now()) " \
          "ON CONFLICT (post_id, user_id) DO NOTHING",
        "likes.insert",
        [sql_bind("post_id", post_id, INT_TYPE), sql_bind("user_id", user_id, INT_TYPE)]
      )
    end

    like_count = LangperfOtel.db_span("DB Q4c like count") do
      conn.select_value(
        "SELECT COUNT(*)::bigint AS like_count FROM likes WHERE post_id = $1",
        "likes.count",
        [sql_bind("post_id", post_id, INT_TYPE)]
      )
    end

    render json: { post_id: post_id, like_count: like_count }
  end
end
