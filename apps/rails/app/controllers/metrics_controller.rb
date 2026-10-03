# frozen_string_literal: true

class MetricsController < ApplicationController
  # GET /metrics — Prometheus text exposition format (version 0.0.4).
  def show
    response.set_header("Content-Type", Prometheus::Client::Formats::Text::CONTENT_TYPE)
    render body: MetricsRegistry.expose
  end
end
