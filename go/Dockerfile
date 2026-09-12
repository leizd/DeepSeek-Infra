FROM golang:1.27.1-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN CGO_ENABLED=0 GOOS=linux go build -ldflags="-w -s" -o /bin/deepseekd ./cmd/deepseekd

FROM alpine:3.20
RUN apk add --no-cache ca-certificates
COPY --from=builder /bin/deepseekd /usr/local/bin/deepseekd
EXPOSE 8090
ENTRYPOINT ["/usr/local/bin/deepseekd"]
