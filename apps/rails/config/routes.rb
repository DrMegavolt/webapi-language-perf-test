Rails.application.routes.draw do
  # langperf benchmark API (see SPEC.md at the repo root)
  get "/feed",            to: "feed#show"
  get "/posts/:id",       to: "posts#show"
  post "/posts",          to: "posts#create"
  post "/posts/:id/like", to: "likes#create"

  get "/healthz", to: "health#show"
  get "/metrics", to: "metrics#show"
end
