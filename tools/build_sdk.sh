#!/usr/bin/env bash
set -euo pipefail

root="$(cd -- "$(dirname -- "$0")/.." && pwd)"
revision="$(cat "$root/SLUICE_REVISION")"
profile="$1"
jobs="$2"
case "$profile" in
    ring) liburing=y ;;
    noring) liburing=n ;;
    *) echo "invalid SDK profile: $profile" >&2; exit 1 ;;
esac
build="$root/build/$profile"
source="$build/sluice"
sdk="$build/sdk"
if [[ -n "${SLUICE_SOURCE:-}" ]]; then
    remote="$(realpath "$SLUICE_SOURCE")"
else
    remote=https://github.com/jnhu76/Sluice.git
fi

mkdir -p "$source" "$sdk"
if [[ ! -d "$source/.git" ]]; then
    git -C "$source" init -q
fi
if ! git -C "$source" cat-file -e "$revision^{commit}" 2>/dev/null; then
    git -C "$source" fetch --depth 1 --no-tags "$remote" "$revision"
fi
git -C "$source" checkout -q --detach "$revision"
if [[ -n "$(git -C "$source" status --porcelain --untracked-files=no)" ]]; then
    echo "Sluice SDK source checkout contains tracked edits" >&2
    exit 1
fi

cd "$source"
xmake f -m release --liburing="$liburing" -y
xmake build -j "$jobs" -y sluice_async
xmake install -o "$sdk" sluice_core
xmake install -o "$sdk" sluice_async
printf '%s\n' "$revision" > "$sdk/.ready"
