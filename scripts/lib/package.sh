#!/usr/bin/env bash
# Deterministic packaging: the same inputs produce the same archive bytes, whoever builds them
# and wherever they build them.
#
# Archives are otherwise full of accidents — file order from readdir, mtimes from checkout time,
# the builder's umask, the gzip header's timestamp — and every one of them makes two honest
# builds of the same commit disagree. That turns "are these the bytes I verified?" into a
# question nobody can answer. These helpers remove each accident in turn.
#
# Source it, do not run it:
#
#   . "$(dirname "$0")/../../scripts/lib/package.sh"
#   pkg_zip    "$STAGE" TakaroPlugin dist/takaro-plugin.zip
#   pkg_tar_gz "$STAGE" TakaroPlugin dist/takaro-plugin.tar.gz
#   pkg_sha256sums dist
#
# `pkg_zip` and `pkg_tar_gz` normalise the staged folder in place; stage a copy, never a source
# tree. Nothing here depends on the caller's working directory.

# 1980-01-01, the earliest timestamp a zip entry can carry.
PKG_MIN_EPOCH=315532800

pkg_abs() {
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *) printf '%s/%s\n' "$(pwd)" "$1" ;;
  esac
}

pkg_repo_root() {
  # The repository this library lives in, independent of who sourced it and from where.
  (cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
}

pkg_epoch() {
  # $SOURCE_DATE_EPOCH, else the commit time of the repository, else the zip floor.
  local candidate
  for candidate in "${SOURCE_DATE_EPOCH:-}" "$(git -C "$(pkg_repo_root)" log -1 --format=%ct 2>/dev/null || true)"; do
    if [ -n "$candidate" ] && [ -z "${candidate//[0-9]/}" ] && [ "$candidate" -ge "$PKG_MIN_EPOCH" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done
  printf '%s\n' "$PKG_MIN_EPOCH"
}

pkg_normalize() {
  # One permission per kind and one timestamp for everything, so neither the builder's umask
  # nor the moment of checkout can reach the archive.
  local dir="${1:?pkg_normalize <dir>}" epoch
  epoch="$(pkg_epoch)"
  find "$dir" -type d -exec chmod 755 {} +
  find "$dir" -type f -exec chmod 644 {} +
  find "$dir" -exec touch -h -d "@$epoch" {} +
}

pkg_zip() {
  # zip -X drops the uid/gid and extended-timestamp extra fields; the sorted file list fixes
  # entry order; feeding only files keeps directory entries (and their own mtimes) out.
  local stage="${1:?pkg_zip <stage> <folder> <out.zip>}" folder="${2:?}" out
  out="$(pkg_abs "${3:?}")"
  pkg_normalize "$stage/$folder"
  mkdir -p "$(dirname "$out")"
  rm -f "$out.tmp.$$"
  (cd "$stage" && find "$folder" -type f | LC_ALL=C sort | zip -X -q "$out.tmp.$$" -@)
  mv "$out.tmp.$$" "$out"
}

pkg_tar_gz() {
  # gzip -n leaves the original name and mtime out of the header, which is the one thing
  # --mtime cannot reach.
  local stage="${1:?pkg_tar_gz <stage> <folder> <out.tar.gz>}" folder="${2:?}" out epoch
  out="$(pkg_abs "${3:?}")"
  epoch="$(pkg_epoch)"
  pkg_normalize "$stage/$folder"
  mkdir -p "$(dirname "$out")"
  (
    cd "$stage" &&
      tar --sort=name --mtime="@$epoch" --owner=0 --group=0 --numeric-owner --format=gnu -cf - "$folder" |
      gzip -n -9 >"$out.tmp.$$"
  )
  mv "$out.tmp.$$" "$out"
}

pkg_sha256sums() {
  # GNU sha256sum format, sorted by name, covering every regular file in the directory but
  # the checksum file itself. The listing is built first and written afterwards: a redirection
  # into the directory being listed would put the half-written file into its own listing.
  local dir="${1:?pkg_sha256sums <dir>}" listing
  listing="$(
    cd "$dir" &&
      find . -maxdepth 1 -type f ! -name SHA256SUMS |
      sed 's|^\./||' |
      LC_ALL=C sort |
      xargs -r sha256sum
  )"
  if [ -n "$listing" ]; then
    printf '%s\n' "$listing" >"$dir/SHA256SUMS"
  else
    : >"$dir/SHA256SUMS"
  fi
}
