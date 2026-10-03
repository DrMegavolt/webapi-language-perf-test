# frozen_string_literal: true

class ApplicationController < ActionController::API
  private

  # Render the SPEC's JSON error shape: {"error": "..."}.
  def render_error(status, message)
    render json: { error: message }, status: status
  end

  # Strict base-10 integer parse; returns nil for anything that is not an
  # integer (used to answer 400 instead of 500 on bad params). JSON request
  # bodies keep their types in params (user_id arrives as Integer), while
  # route/query params are Strings — accept both, reject everything else.
  def parse_integer(value)
    case value
    when Integer then value
    when String  then Integer(value, 10)
    else nil
    end
  rescue ArgumentError
    nil
  end

  # Bind helper for raw SQL with positional $1..$n placeholders.
  def sql_bind(name, value, type)
    ActiveRecord::Relation::QueryAttribute.new(name, value, type)
  end

  INT_TYPE  = ActiveRecord::Type::Integer.new
  TEXT_TYPE = ActiveRecord::Type::Text.new

  # ISO-8601 UTC with microseconds, e.g. 2026-01-02T03:04:05.123456+00:00.
  def iso8601_utc(value)
    case value
    when Time   then value.getutc.iso8601(6)
    when String then Time.parse(value).getutc.iso8601(6)
    else value.to_s
    end
  end
end
