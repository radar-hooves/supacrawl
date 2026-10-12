#!/usr/bin/env bash
# Publish one uv member's wheel to the household index, versioned by the AEST committer
# time of the last commit that changed its directory: unchanged content always builds the
# same version, so a version the index already holds (409) is done and a re-run is safe.
# Usage: wheel.sh <member-dir> [name sha]   (the deploy signature; name and sha are unused.
# Needs the member's history: not a shallow clone.)
. "$(dirname "$0")/lib.sh"
need CHANGE_INDEX_URL CHANGE_INDEX_USER CHANGE_INDEX_PASSWORD_FILE
[ -r "$CHANGE_INDEX_PASSWORD_FILE" ] || die "CHANGE_INDEX_PASSWORD_FILE is not readable by $(id -un)"
ws "${1:-.}"
[ "$(git -C "$dir" rev-parse --is-shallow-repository)" = false ] || die "a shallow clone cannot date $name's last change"
version=$(cd "$dir" && TZ=Australia/Brisbane git log -1 --format=%cd --date=format-local:%Y.%-m.%-d%H%M%S -- .)
[[ $version =~ ^[0-9]{4}\.([1-9]|1[0-2])\.[1-9][0-9]*$ ]] || die "$name: '$version' is not YYYY.M.N"
out=$(mktemp -d)
cp "$dir/pyproject.toml" "$out/"
trap 'cp "$out/pyproject.toml" "$dir/" && rm -rf "$out"' EXIT
(cd "$root" && uv version --frozen --package "$name" "$version" && uv build --package "$name" --wheel --out-dir "$out/dist")
whl=$(echo "$out"/dist/*.whl)
[ "$(basename "$whl" | cut -d- -f2)" = "$version" ] || die "$whl does not carry the version $version"
export TWINE_PASSWORD
TWINE_PASSWORD=$(<"$CHANGE_INDEX_PASSWORD_FILE")
[ -z "${GITHUB_ACTIONS:-}" ] || echo "::add-mask::$TWINE_PASSWORD"
if log=$(uvx --from 'twine==7.0.0' twine upload --non-interactive --disable-progress-bar \
  --repository-url "$CHANGE_INDEX_URL" -u "$CHANGE_INDEX_USER" "$whl" 2>&1); then
  printf '%s\n' "$log"
elif grep -qE 'HTTPError: 409' <<<"$log"; then
  note "$name $version is already on the index"
else
  printf '%s\n' "$log" >&2 && exit 1
fi
