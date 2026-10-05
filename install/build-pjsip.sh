#!/usr/bin/env bash
# Build PJSIP (pjproject) with TLS and SRTP, then prove both are in the binary.
#
# The commonly copy-pasted incantation is
#
#     ./configure --with-ssl --with-srtp
#
# and the second flag does not exist: pjproject's own SRTP switches are
# --disable-srtp / --with-external-srtp, and libsrtp is bundled and built by
# default. `./configure --with-srtp` is silently accepted (autoconf ignores
# unknown --with-* options), which is why so many builds believe they have
# SRTP and do not. This script therefore *checks the built binary* for
# --use-srtp and --use-tls and fails loudly if either is missing. A SIP client
# that cannot do SRTP should not start at all.
#
#   sudo install/build-pjsip.sh                 # build and install to /usr/local
#   sudo PJPROJECT_REF=2.15.1 install/build-pjsip.sh   # pin a release tag
#   install/build-pjsip.sh --print-steps        # show what would run
#
# Notes:
#  * Audio is useful (this is a phone), so we keep ALSA support. Pass
#    --no-audio for a headless build that will use --null-audio.
#  * Video is disabled: this terminal has no camera, and every disabled parser
#    is a CVE surface you do not have to track.

set -euo pipefail

SRC_DIR="${PJPROJECT_SRC:-/usr/local/src/pjproject}"
PREFIX="${PJPROJECT_PREFIX:-/usr/local}"
PJPROJECT_REPO="${PJPROJECT_REPO:-https://github.com/pjsip/pjproject.git}"
PJPROJECT_REF="${PJPROJECT_REF:-master}"
JOBS="${JOBS:-$(nproc 2>/dev/null || echo 2)}"
PRINT_ONLY=0
WITH_AUDIO=1

while [ $# -gt 0 ]; do
    case "$1" in
        --print-steps) PRINT_ONLY=1; shift ;;
        --no-audio)    WITH_AUDIO=0; shift ;;
        --prefix)      PREFIX="${2:?}"; shift 2 ;;
        --ref)         PJPROJECT_REF="${2:?}"; shift 2 ;;
        --src)         SRC_DIR="${2:?}"; shift 2 ;;
        -h|--help)     awk 'NR==1{next} /^#/{sub(/^# ?/,""); print; next} {exit}' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

info() { printf '[*] %s\n' "$*" >&2; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
run() {
    if [ "$PRINT_ONLY" -eq 1 ]; then printf '    %s\n' "$*" >&2; return 0; fi
    printf '    $ %s\n' "$*" >&2
    "$@"
}

CONFIGURE_FLAGS=(
    "--prefix=$PREFIX"
    --enable-shared
    --disable-video
    --disable-opencore-amr
    --disable-speex-codec
    --disable-g7221-codec
    --disable-libyuv
    --with-ssl
)
[ "$WITH_AUDIO" -eq 1 ] || CONFIGURE_FLAGS+=(--disable-sound)
[ "$WITH_AUDIO" -eq 0 ] || CONFIGURE_FLAGS+=(--disable-v4l2)

if [ "$PRINT_ONLY" -eq 1 ]; then
    info "steps that would run (nothing is executed):"
    printf '  1. apt-get install build-essential libssl-dev libasound2-dev git curl\n' >&2
    printf '  2. git clone %s %s && git checkout %s\n' "$PJPROJECT_REPO" "$SRC_DIR" "$PJPROJECT_REF" >&2
    printf '  3. ./configure %s\n' "${CONFIGURE_FLAGS[*]}" >&2
    printf '  4. make dep && make -j%s\n' "$JOBS" >&2
    printf '  5. make install && ldconfig\n' >&2
    printf '  6. verify pjsua --help advertises --use-tls and --use-srtp\n' >&2
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "installing to $PREFIX needs root (run with sudo), or pass --print-steps"

info "installing build dependencies"
if command -v apt-get >/dev/null 2>&1; then
    DEPS=(build-essential git curl libssl-dev)
    [ "$WITH_AUDIO" -eq 1 ] && DEPS+=(libasound2-dev)
    run apt-get update -qq
    run apt-get install -y --no-install-recommends "${DEPS[@]}"
else
    info "not a Debian-style system: ensure gcc, make, git, OpenSSL headers${WITH_AUDIO:+ and ALSA headers} are present"
fi

for tool in gcc make git; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is required"
done

info "fetching pjproject ($PJPROJECT_REF) into $SRC_DIR"
mkdir -p "$(dirname "$SRC_DIR")"
if [ -d "$SRC_DIR/.git" ]; then
    run git -C "$SRC_DIR" fetch --tags --depth 1 origin "$PJPROJECT_REF"
    run git -C "$SRC_DIR" checkout --force FETCH_HEAD
    run git -C "$SRC_DIR" submodule update --init --depth 1
else
    run git clone --depth 1 --branch "$PJPROJECT_REF" "$PJPROJECT_REPO" "$SRC_DIR"
fi

info "configuring"
(
    cd "$SRC_DIR"
    run ./configure "${CONFIGURE_FLAGS[@]}"
)

info "building with $JOBS job(s) -- this takes a few minutes"
(
    cd "$SRC_DIR"
    run make dep
    run make -j"$JOBS"
)

# Verify *before* installing: a build without TLS or SRTP must not be put on
# the system, because pjsua would then fail at run time (or worse, succeed
# without encryption if the launcher checks were skipped).
info "verifying the built binary supports TLS and SRTP"
BIN="$SRC_DIR/pjsip-apps/src/pjsua"
if [ -x "$BIN/pjsua" ]; then
    HELP="$("$BIN/pjsua" --help 2>&1 || true)"
    case "$HELP" in
        *--use-tls*)  info "TLS support present" ;;
        *) die "built pjsua has no TLS support: OpenSSL headers were probably missing. Install libssl-dev and rebuild." ;;
    esac
    case "$HELP" in
        *--use-srtp*) info "SRTP support present" ;;
        *) die "built pjsua has no SRTP support: libsrtp was not compiled in. Do not use this build. Rebuild from a clean tree (make distclean) and check that third_party/build/libsrtp was built." ;;
    esac
else
    die "build produced no pjsua binary at $BIN"
fi

info "installing to $PREFIX"
(
    cd "$SRC_DIR"
    run make install
)
if command -v ldconfig >/dev/null 2>&1; then
    run ldconfig
fi

info "done"
info "verify on your PATH with:  phone sip --check   (or: pjsua --help | grep -E -- '--use-tls|--use-srtp')"
info "a pinned release is better for reproducibility: PJPROJECT_REF=<tag> $0"
