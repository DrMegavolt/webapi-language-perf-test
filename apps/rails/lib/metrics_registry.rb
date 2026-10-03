# frozen_string_literal: true

require "prometheus/client"
require "prometheus/client/formats/text"

# Process-wide Prometheus registry singleton for the langperf benchmark.
#
# Holds the three metric families required by the SPEC:
#   * Histogram http_request_duration_seconds  labels: method, route, status
#   * Counter   http_requests_total            labels: method, route, status
#   * Gauge     app_memory_rss_bytes           (no labels)
#
# The app runs as a single Puma process, so the client's default in-process
# ("Synchronized") store is correct and cheap.
module MetricsRegistry
  BUCKETS = [0.0005, 0.001, 0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10].freeze

  ROUTE_FEED         = "/feed"
  ROUTE_POSTS        = "/posts"
  ROUTE_POST_ID      = "/posts/:id"
  ROUTE_POST_ID_LIKE = "/posts/:id/like"

  # /proc/self/status line: "VmRSS:\t  123456 kB"
  VM_RSS_RE = /\AVmRSS:\s+(\d+)\s+kB\z/.freeze

  module_function

  def registry
    @registry ||= Prometheus::Client::Registry.new
  end

  def http_request_duration_seconds
    @http_request_duration_seconds ||= Prometheus::Client::Histogram.new(
      :http_request_duration_seconds,
      docstring: "HTTP request duration in seconds",
      labels: %i[method route status],
      buckets: BUCKETS
    ).tap { |metric| registry.register(metric) }
  end

  def http_requests_total
    @http_requests_total ||= Prometheus::Client::Counter.new(
      :http_requests_total,
      docstring: "Total HTTP requests",
      labels: %i[method route status]
    ).tap { |metric| registry.register(metric) }
  end

  def app_memory_rss_bytes
    @app_memory_rss_bytes ||= Prometheus::Client::Gauge.new(
      :app_memory_rss_bytes,
      docstring: "Process resident set size in bytes (VmRSS from /proc/self/status)"
    ).tap { |metric| registry.register(metric) }
  end

  # Map (HTTP method, path) -> route-pattern label. Numeric segments must be
  # numeric to match. Returns nil for everything that must NOT be recorded:
  # /metrics, /healthz and unmatched paths.
  def route_for(method, path)
    case method
    when "GET"
      case path
      when ROUTE_FEED              then ROUTE_FEED
      when /\A\/posts\/\d+\z/      then ROUTE_POST_ID
      end
    when "POST"
      case path
      when ROUTE_POSTS                  then ROUTE_POSTS
      when /\A\/posts\/\d+\/like\z/     then ROUTE_POST_ID_LIKE
      end
    end
  end

  # Observe one request exactly once: histogram + counter with shared labels.
  def record(method:, route:, status:, duration:)
    labels = { method: method, route: route, status: status }
    http_request_duration_seconds.observe(duration, labels: labels)
    http_requests_total.increment(labels: labels)
  end

  # Resident set size in bytes from /proc/self/status (VmRSS kB x 1024).
  # Returns nil when unavailable (e.g. non-Linux development machine).
  def read_rss_bytes
    File.foreach("/proc/self/status") do |line|
      if (match = VM_RSS_RE.match(line.chomp))
        return match[1].to_i * 1024
      end
    end
    nil
  rescue StandardError
    nil
  end

  # Update app_memory_rss_bytes on every recorded request.
  def update_memory_gauge
    if (bytes = read_rss_bytes)
      app_memory_rss_bytes.set(bytes)
    end
  end

  # Pre-create the happy-path time series at boot so /metrics always exposes
  # both metric families, even before the first request.
  def warmup
    http_request_duration_seconds
    http_requests_total
    app_memory_rss_bytes

    %w[GET POST].each do |method|
      routes = method == "GET" ? [ROUTE_FEED, ROUTE_POST_ID] : [ROUTE_POSTS, ROUTE_POST_ID_LIKE]
      routes.each do |route|
        labels = { method: method, route: route, status: "200" }
        http_request_duration_seconds.init_label_set(labels)
        http_requests_total.init_label_set(labels)
      end
    end
  end

  # Prometheus text exposition (version 0.0.4) of the whole registry.
  # (prometheus-client v5: Formats::Text.marshal replaced text_for_registry.)
  def expose
    Prometheus::Client::Formats::Text.marshal(registry)
  end
end
