# frozen_string_literal: true

# Warm the global Prometheus registry at boot so /metrics always exposes both
# metric families (zero-valued 200 series for the four benchmark routes).
#
# after_initialize runs after eager loading, so the zeitwerk-managed constant
# is available (autoloading during config/initializers is not allowed).
Rails.application.config.after_initialize do
  MetricsRegistry.warmup
end
