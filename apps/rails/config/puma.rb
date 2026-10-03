# Single-process Puma configuration for the langperf benchmark.
#
# WEB_CONCURRENCY=0 -> no cluster mode: ONE process serves all requests
# (required by the benchmark so /metrics lives in the same process registry).
# RAILS_MAX_THREADS -> thread pool size; Active Record pool follows it via
# database.yml (max_connections), staying at 8 per the SPEC.

max_threads_count = ENV.fetch("RAILS_MAX_THREADS", 8)
min_threads_count = ENV.fetch("RAILS_MIN_THREADS", max_threads_count)
threads min_threads_count, max_threads_count

web_concurrency = Integer(ENV.fetch("WEB_CONCURRENCY", 0))
workers web_concurrency if web_concurrency > 0

# Bind 0.0.0.0:8080 (never 127.0.0.1), overridable via PORT.
port ENV.fetch("PORT", 8080)

environment ENV.fetch("RACK_ENV", "development")

pidfile ENV["PIDFILE"] if ENV["PIDFILE"]
