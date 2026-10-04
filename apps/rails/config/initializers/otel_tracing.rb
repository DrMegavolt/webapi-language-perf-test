# frozen_string_literal: true

# OpenTelemetry tracing (contract shared across all langperf stacks): one fresh
# SERVER root span per API request ("HTTP <METHOD> <route>") and one CLIENT
# child span per SQL statement ("DB Q1 feed" etc.). No context extraction is
# performed, so incoming trace headers are never propagated — roots are always
# fresh. Spans are parented explicitly via Thread.current (cleared in ensure),
# so no context-manager attach/detach is needed and spans never leak across
# Puma threads.
#
# Export: OTLP/HTTP (protobuf) to <OTEL_EXPORTER_OTLP_ENDPOINT>/v1/traces,
# batched every 500ms (prompt flush), 100% sampled.
require "opentelemetry/sdk"
require "opentelemetry/exporter/otlp"

module LangperfOtel
  EXPORTER_ENDPOINT = ENV.fetch(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://otel-gateway-collector.observability.svc.cluster.local:4318"
  ).sub(%r{/+\z}, "").freeze

  # The exporter uses a passed `endpoint:` verbatim (the path is NOT appended),
  # so build the full signal URL here — POST <endpoint>/v1/traces.
  TRACER_PROVIDER = OpenTelemetry::SDK::Trace::TracerProvider.new(
    resource: OpenTelemetry::SDK::Resources::Resource.create(
      "service.name" => "langperf-rails"
    ),
    sampler: OpenTelemetry::SDK::Trace::Samplers::ALWAYS_ON # 100% sampling
  ).tap do |provider|
    provider.add_span_processor(
      OpenTelemetry::SDK::Trace::Export::BatchSpanProcessor.new(
        OpenTelemetry::Exporter::OTLP::Exporter.new(
          endpoint: "#{EXPORTER_ENDPOINT}/v1/traces"
        ),
        schedule_delay: 500 # milliseconds
      )
    )
  end

  OpenTelemetry.tracer_provider = TRACER_PROVIDER

  TRACER = TRACER_PROVIDER.tracer("langperf.rails")

  module_function

  # SERVER root span around the whole request handling: started before the app
  # runs, finished after the response (even on exceptions). While active, the
  # span is exposed to controllers via Thread.current for explicit parenting.
  def with_request_span(method, route)
    span = TRACER.start_span(
      "HTTP #{method} #{route}",
      kind: :server,
      attributes: { "http.method" => method, "http.route" => route }
    )
    previous = Thread.current[:langperf_otel_span]
    Thread.current[:langperf_otel_span] = span
    begin
      yield
    ensure
      Thread.current[:langperf_otel_span] = previous
      span.finish
    end
  end

  # CLIENT span around one DB round-trip (wall time start -> end), parented
  # explicitly on the request's SERVER span. Returns the block's value.
  def db_span(name)
    parent = Thread.current[:langperf_otel_span]
    span = TRACER.start_span(
      name,
      kind: :client,
      attributes: { "db.system" => "postgresql" },
      with_parent: parent ? OpenTelemetry::Trace.context_with_span(parent) : nil
    )
    begin
      yield
    rescue Exception => e
      span.record_exception(e)
      span.status = OpenTelemetry::Trace::Status.error(e.message)
      raise
    ensure
      span.finish
    end
  end

  # Flush pending spans promptly when the process exits.
  at_exit { TRACER_PROVIDER.shutdown }
end
