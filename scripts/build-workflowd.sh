#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
GO_BIN="${GO_BIN:-go}"
VERSION="${VERSION:-3.3.0}"
OUTPUT_DIR="${ROOT_DIR}/lib/workflowd"

command -v "${GO_BIN}" >/dev/null 2>&1 || {
  printf 'Go compiler not found: %s\n' "${GO_BIN}" >&2
  exit 1
}

install -d -m 0755 "${OUTPUT_DIR}"
for arch in amd64 arm64; do
  output="${OUTPUT_DIR}/llm-workflowd-linux-${arch}"
  (
    cd "${ROOT_DIR}/workflowd"
    CGO_ENABLED=0 GOOS=linux GOARCH="${arch}" \
      "${GO_BIN}" build -trimpath -buildvcs=false \
      -ldflags "-s -w -buildid= -X main.buildVersion=${VERSION}" \
      -o "${output}" .
  )
  chmod 0755 "${output}"
done

{
  printf 'version=%s\n' "${VERSION}"
  printf 'go=%s\n' "$("${GO_BIN}" version)"
  for arch in amd64 arm64; do
    file="${OUTPUT_DIR}/llm-workflowd-linux-${arch}"
    if command -v sha256sum >/dev/null 2>&1; then
      digest=$(sha256sum "${file}" | awk '{print $1}')
    else
      digest=$(shasum -a 256 "${file}" | awk '{print $1}')
    fi
    printf 'linux-%s=%s\n' "${arch}" "${digest}"
  done
} >"${OUTPUT_DIR}/checksums.env"
chmod 0644 "${OUTPUT_DIR}/checksums.env"

printf 'Built LLMCtl workflow runtime %s in %s\n' "${VERSION}" "${OUTPUT_DIR}"
