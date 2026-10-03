# Multi-stage image for the langperf benchmark (linux/amd64).
# Build stage compiles native gems; final stage is runtime-only (no compilers).

FROM ruby:3.3-slim AS build

ENV BUNDLE_DEPLOYMENT=1 \
    BUNDLE_PATH=/usr/local/bundle \
    BUNDLE_WITHOUT="development:test" \
    BUNDLE_JOBS=4 \
    BUNDLE_RETRY=3

RUN apt-get update -qq && \
    apt-get install --no-install-recommends -y build-essential libpq-dev && \
    rm -rf /var/lib/apt/lists /var/cache/apt/archives

WORKDIR /app

COPY Gemfile Gemfile.lock ./
RUN bundle install && \
    rm -rf "${BUNDLE_PATH}"/ruby/*/cache

COPY . .

FROM ruby:3.3-slim

RUN apt-get update -qq && \
    apt-get install --no-install-recommends -y libpq5 && \
    rm -rf /var/lib/apt/lists /var/cache/apt/archives

WORKDIR /app

ENV RAILS_ENV=production \
    RACK_ENV=production \
    BUNDLE_DEPLOYMENT=1 \
    BUNDLE_PATH=/usr/local/bundle \
    BUNDLE_WITHOUT="development:test" \
    RUBY_YJIT_ENABLE=1 \
    WEB_CONCURRENCY=0 \
    RAILS_MAX_THREADS=8 \
    SECRET_KEY_BASE_DUMMY=1

COPY --from=build /usr/local/bundle /usr/local/bundle
COPY --from=build /app /app

EXPOSE 8080

CMD ["bundle", "exec", "puma", "-C", "config/puma.rb"]
