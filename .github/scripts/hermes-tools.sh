#!/usr/bin/env bash
# Run Hermes' own plugin checks against this checkout.
# Usage: hermes-tools.sh <path to hermes executable>
# The plugin is copied into a temporary HERMES_HOME and enabled through config.yaml;
# `hermes plugins enable` is not used because it needs network access.
set -euo pipefail

hermes="$1"
root="$(cd "$(dirname "$0")/../.." && pwd)"
home="$(mktemp -d)"
dest="$home/plugins/degeneracy-guard"
mkdir -p "$dest"
cp "$root"/*.py "$root/plugin.yaml" "$dest/"
printf 'plugins:\n  enabled:\n    - degeneracy-guard\n' > "$home/config.yaml"
export HERMES_HOME="$home" DEGENERACY_GUARD_HOME="$home/state"

echo "::group::hermes plugins validate"
"$hermes" plugins validate "$dest"
echo "::endgroup::"
echo "::group::hermes plugins doctor degeneracy-guard --ci"
"$hermes" plugins doctor degeneracy-guard --ci
echo "::endgroup::"
