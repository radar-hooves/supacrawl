#!/usr/bin/env bash
# An MCP server's deploy: its wheel, pinning its whole runtime closure exactly, and the
# wheels of the workspace libraries it pins, handed to this host's CHANGE_MCP_RELEASE,
# which gives the server a venv of its own and keeps it only if the server stays up. The
# wheels reach the index only after that release succeeds, so a host following the index
# installs only bytes this one has run.
# A wheel's version is the AEST committer time of the last commit that changed what it
# carries: the server's directory, the lock and its libraries for the server; its own
# directory for a library.
# Usage: mcp.sh <member-dir> <name> [sha]   (needs the member's history: not a shallow clone)
. "$(dirname "$0")/lib.sh"
need CHANGE_MCP_RELEASE CHANGE_INDEX_URL CHANGE_INDEX_USER CHANGE_INDEX_PASSWORD_FILE
[ -r "$CHANGE_INDEX_PASSWORD_FILE" ] || die "CHANGE_INDEX_PASSWORD_FILE is not readable by $(id -un)"
ws "${1:-.}"
server=$(basename "$dir") sha=${3:-$(git -C "$dir" rev-parse HEAD)}
[ "$(git -C "$dir" rev-parse --is-shallow-repository)" = false ] || die "a shallow clone cannot date $name's last change"
stamp() { (cd "$root" && TZ=Australia/Brisbane git log -1 --format=%cd --date=format-local:%Y.%-m.%-d%H%M%S -- "$@"); }
slug=${GITHUB_REPOSITORY:-$(cd "$root" && gh repo view --json nameWithOwner -q .nameWithOwner)}

out=$(mktemp -d) touched=() libs=()
restore() {
  for i in "${!touched[@]}"; do cp "$out/pyproject.$i" "${touched[i]}"; done
  rm -rf "$out"
}
trap restore EXIT
build() {  # <name> <dir> <version>
  cp "$2/pyproject.toml" "$out/pyproject.${#touched[@]}" && touched+=("$2/pyproject.toml")
  (cd "$root" && uv version --frozen --package "$1" "$3" >/dev/null &&
    uv build --package "$1" --wheel --out-dir "$out/dist")
}

args=(--frozen --no-hashes --no-header --no-annotate --package "$name" --all-extras --no-default-groups)
(cd "$root" && uv export "${args[@]}" --no-emit-workspace) | grep -v '^[[:space:]]*\(#\|$\)' >"$out/pins"
meta=$(cd "$root" && uv workspace metadata --preview-features workspace-metadata)
# Captured first: a process substitution's failure never reaches set -e.
(cd "$root" && uv export "${args[@]}") >"$out/closure"
while read -r rel; do
  lib=$(cd "$root/$rel" && pwd -P)
  [ "$lib" != "$dir" ] || continue
  libname=$(jq -r --arg p "$lib" '.members[] | select(.path == $p) | .name' <<<"$meta")
  [ -n "$libname" ] || die "$rel is in $name's closure but is no workspace member"
  # One recipe per name: a server's wheel is pinned, a library's is not.
  [ ! -f "$lib/server_config.json" ] || [ -f "$lib/Dockerfile" ] ||
    die "$name uses the server $libname as a library; a server released here cannot also ship as one"
  libs+=("${lib#"$root"/}")
  v=$(stamp "${lib#"$root"/}")
  build "$libname" "$lib" "$v"
  echo "$libname==$v" >>"$out/pins"
done < <(sed -n 's/^-e \([^ ;]*\).*/\1/p' "$out/closure")

# The server's wheel carries its pins, so its version moves with any library it pins too.
version=$(stamp "${dir#"$root"/}" uv.lock "${libs[@]}")
# A re-run of an older run must never roll this host's server back; the same version
# releases again harmlessly. A release is named <version>-<hash>.
if live=$(readlink -e "/var/lib/mcp-release/servers/$server/current"); then
  live=${live##*/} && live=${live%%-*}
  [ "$(printf '%s\n' "$live" "$version" | sort -V | tail -1)" = "$version" ] ||
    die "$(uname -n) runs $server $live, which is newer than $version; refusing to roll it back, nothing released"
fi
build "$name" "$dir" "$version"
whl=$(echo "$out/dist/$(tr - _ <<<"$name")-$version-"*.whl)
[ -f "$whl" ] || die "no wheel for $name $version in $out/dist"
python3 "$here/pin_wheel.py" "$whl" "$out/pins" "https://github.com/$slug/commit/$sha"

note "releasing $server: $name $version on $(uname -n)"
"$CHANGE_MCP_RELEASE" "$server" "$out"/dist/*.whl || die "$server did not release here, so nothing was published"

[ -z "${GITHUB_ACTIONS:-}" ] || echo "::add-mask::$(<"$CHANGE_INDEX_PASSWORD_FILE")"
for w in "$out"/dist/*.whl; do
  if log=$(TWINE_PASSWORD=$(<"$CHANGE_INDEX_PASSWORD_FILE") uvx --from 'twine==7.0.0' twine upload \
    --non-interactive --disable-progress-bar --repository-url "$CHANGE_INDEX_URL" -u "$CHANGE_INDEX_USER" "$w" 2>&1); then
    note "published $(basename "$w")"
  elif grep -qE 'HTTPError: 409' <<<"$log"; then
    note "$(basename "$w") is already on the index"
  else
    printf '%s\n' "$log" >&2 && die "$server is live here but $(basename "$w") did not publish"
  fi
done
