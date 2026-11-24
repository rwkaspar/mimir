#!/bin/bash
# Run the whole test suite the way CI does: unit tests, the static guards,
# the HTTP tests and the phone pages in a real (headless, mobile) browser.
#
#   scripts/test.sh              everything
#   scripts/test.sh tests/browser -k library     pytest arguments pass through
#
# Screenshots of every page land in tests/browser/shots/ — look at them.
set -e
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${MIMIR_TEST_IMAGE:-mimir-test-full}"
if [ -z "$MIMIR_TEST_NO_BUILD" ]; then
    docker build -q -t "$IMAGE" -f "$REPO/tests/Dockerfile" "$REPO" >/dev/null
fi
[ $# -gt 0 ] || set -- tests
exec timeout -k 10 1200 docker run --rm --init --ipc=host -v "$REPO":/repo "$IMAGE" python -m pytest -q "$@"
