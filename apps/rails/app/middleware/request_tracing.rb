# frozen_string_literal: true

# Rack middleware that owns the tracing lifecycle per request (contract shared
# across all langperf stacks): one fresh SERVER root span per API request,
# started before the app runs and closed after the response returns, so the
# span measures wall time around the whole request handling.
#
# Inserted just before (i.e. outside) RequestMetrics in the stack. Route
# normalization reuses MetricsRegistry.route_for so the four span routes are
# exactly the four metrics route patterns; /metrics, /healthz (and unmatched
# paths) get no spans. Incoming trace headers are never extracted — roots are
# always fresh.
class RequestTracing
  def initialize(app)
    @app = app
  end

  def call(env)
    method = env["REQUEST_METHOD"]
    route = MetricsRegistry.route_for(method, env["PATH_INFO"])
    return @app.call(env) unless route

    LangperfOtel.with_request_span(method, route) { @app.call(env) }
  end
end
