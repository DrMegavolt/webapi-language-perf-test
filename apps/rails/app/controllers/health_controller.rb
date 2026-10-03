# frozen_string_literal: true

class HealthController < ApplicationController
  # GET /healthz — 200 only after a successful SELECT 1 against the DB.
  def show
    ActiveRecord::Base.connection.select_value("SELECT 1")
    render json: { status: "ok" }
  end
end
