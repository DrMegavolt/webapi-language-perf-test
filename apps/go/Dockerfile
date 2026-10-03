# syntax=docker/dockerfile:1
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -trimpath -ldflags="-s -w" -o /out/langperf-api .

FROM scratch
ENV GOMAXPROCS=1
EXPOSE 8080
COPY --from=build /out/langperf-api /langperf-api
ENTRYPOINT ["/langperf-api"]
