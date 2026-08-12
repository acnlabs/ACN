#!/usr/bin/env bash
# Apply the GitHub repository topics and description used by the Product Hunt
# launch copy (docs/product-hunt/launch-copy.md).
#
# Preview (default):
#   ./scripts/apply_repo_metadata.sh
#
# Apply:
#   ./scripts/apply_repo_metadata.sh --execute
#
# Requires the gh CLI authenticated with a token that has write access to the
# repository (`gh auth login`, or GH_TOKEN with the `repo` scope).
set -euo pipefail

REPO="${REPO:-acnlabs/ACN}"
DESCRIPTION="Open-source infrastructure for AI agents to collaborate - registry, A2A communication, task pool, payments"
TOPICS=(
  ai-agent
  agent-collaboration
  a2a-protocol
  agent-framework
  open-source
)

EXECUTE=0
if [[ "${1:-}" == "--execute" ]]; then
  EXECUTE=1
elif [[ -n "${1:-}" ]]; then
  echo "Unknown argument: $1" >&2
  echo "Usage: $0 [--execute]" >&2
  exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "gh CLI not found. Install it from https://cli.github.com/" >&2
  exit 1
fi

echo "Repository:  ${REPO}"
echo
echo "Current:"
gh repo view "${REPO}" --json description,repositoryTopics \
  --jq '"  description: \(.description // "(none)")\n  topics:      \((.repositoryTopics // []) | map(.name) | join(", ") | if . == "" then "(none)" else . end)"'

echo
echo "Desired:"
echo "  description: ${DESCRIPTION}"
echo "  topics:      $(IFS=', '; echo "${TOPICS[*]}")"

if [[ "${EXECUTE}" -eq 0 ]]; then
  echo
  echo "Dry run. Re-run with --execute to apply."
  exit 0
fi

topic_args=()
for topic in "${TOPICS[@]}"; do
  topic_args+=(--add-topic "${topic}")
done

echo
echo "Applying..."
gh repo edit "${REPO}" --description "${DESCRIPTION}" "${topic_args[@]}"

echo
echo "Result:"
gh repo view "${REPO}" --json description,repositoryTopics \
  --jq '"  description: \(.description)\n  topics:      \((.repositoryTopics // []) | map(.name) | join(", "))"'
