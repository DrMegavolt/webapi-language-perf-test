# frozen_string_literal: true

class FeedController < ApplicationController
  # GET /feed?page=N — 20 posts per page, newest first.
  def show
    page = params.key?(:page) ? parse_integer(params[:page]) : 1
    return render_error(:bad_request, "invalid page") if page.nil? || page < 1

    # The page number is bound as $1 and the offset is computed in SQL,
    # OFFSET ($1 - 1) * 20 — exactly the canonical Q1 from sql/queries.sql.
    rows = ActiveRecord::Base.connection.exec_query(<<~SQL.squish, "feed", [sql_bind("page", page, INT_TYPE)])
      SELECT p.id, p.user_id, u.username, p.content, p.created_at,
             COUNT(l.id)::bigint AS like_count
      FROM posts p
      JOIN users u ON u.id = p.user_id
      LEFT JOIN likes l ON l.post_id = p.id
      GROUP BY p.id, p.user_id, u.username, p.content, p.created_at
      ORDER BY p.created_at DESC, p.id DESC
      LIMIT 20 OFFSET ($1 - 1) * 20
    SQL

    posts = rows.map do |row|
      {
        "id" => row["id"],
        "user_id" => row["user_id"],
        "username" => row["username"],
        "content" => row["content"],
        "like_count" => row["like_count"],
        "created_at" => iso8601_utc(row["created_at"])
      }
    end

    render json: { page: page, posts: posts }
  end
end
