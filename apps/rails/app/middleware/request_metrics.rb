# frozen_string_literal: true

# Rack middleware that measures the full request handling time (body parse +
# DB time included) and records exactly one observation per handled request.
#
# Inserted before Rack::Runtime, so it wraps everything inside the Rails
# middleware stack. /metrics and /healthz (and unmatched paths) are skipped.
class RequestMetrics
  def initialize(app)
    @app = app
  end

  def call(env)
    started = Process.clock_gettime(Process::CLOCK_MONOTONIC)
    status, headers, body = @app.call(env)
    elapsed = Process.clock_gettime(Process::CLOCK_MONOTONIC) - started

    route = MetricsRegistry.route_for(env["REQUEST_METHOD"], env["PATH_INFO"])
    if route
      MetricsRegistry.record(
        method: env["REQUEST_METHOD"],
        route: route,
        status: status.to_s,
        duration: elapsed
      )
      # RAM tracking: refresh the RSS gauge on every recorded request.
      MetricsRegistry.update_memory_gauge
    end

    [status, headers, body]
  end
end
