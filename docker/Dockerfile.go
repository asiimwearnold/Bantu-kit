# Example sandbox for Go. Build: docker build -t bantu-go -f docker/Dockerfile.go docker/
# Project config: "test_cmd": "go test ./...", "sandbox": {"image": "bantu-go", "allow": ["go", "gofmt"]}
FROM golang:1.23
ENV GOFLAGS=-mod=mod GOCACHE=/tmp/gocache GOPATH=/tmp/gopath
WORKDIR /work
