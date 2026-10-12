#!/usr/bin/env bash
# A Go member's leg: `go vet`, then `go test`, over its packages. go comes from PATH, else
# from nixpkgs; cgo stays off, since the runner carries no C toolchain.
# Usage: go.sh <member-dir>
. "$(dirname "$0")/lib.sh"
dir=$(cd "${1:-.}" && pwd)
go=(go)
command -v go >/dev/null || go=(nix shell nixpkgs#go -c go)
export CGO_ENABLED=${CGO_ENABLED:-0}
(cd "$dir" && "${go[@]}" vet ./... && "${go[@]}" test ./...)
