#!/usr/bin/env bash
# Commit results/ as runs/<run_number>.<attempt>-<short_sha>/ on the branch ci-mac-results and push it.
# Plain pushes only: no force, no orphan, no history rewrite. Each run adds one commit that only
# adds its own folder, so two runs never touch the same files; on a push race the commit is
# replayed on top of the new tip and pushed again (max 3 tries).
# Read back with: git fetch origin ci-mac-results && git show origin/ci-mac-results:runs/<dir>/summary.json
set -euo pipefail

BRANCH="ci-mac-results"
RESULTS="${RESULTS:-results}"
RUN_DIR="${RUN_DIR:-runs/${GITHUB_RUN_NUMBER:-local}.${GITHUB_RUN_ATTEMPT:-1}-$(git rev-parse --short=7 HEAD)}"
WT="$(mktemp -d)/results-worktree"

[ -d "$RESULTS" ] || { echo "no $RESULTS folder: nothing to publish"; exit 0; }

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
    git fetch --depth=1 origin "$BRANCH"
    git worktree add --detach "$WT" FETCH_HEAD
else
    # first run ever: a normal new branch starting at the commit under test
    git worktree add --detach "$WT" HEAD
fi

mkdir -p "$WT/$RUN_DIR"
cp -R "$RESULTS"/. "$WT/$RUN_DIR"/
# nothing heavy or hidden in git: no audio but the 6 s test voice, no stray PNG temp files
find "$WT/$RUN_DIR" -name '.*.png' -delete
ALL_OK="$(/usr/bin/python3 -c "import json;print(json.load(open('$WT/$RUN_DIR/summary.json')).get('all_ok'))" 2>/dev/null || echo unknown)"

git -C "$WT" add "$RUN_DIR"
git -C "$WT" commit -q -m "CI mac results: run ${GITHUB_RUN_NUMBER:-local} ($(git rev-parse --short=7 HEAD)) all_ok=$ALL_OK" \
    -m "Workflow run: ${GITHUB_SERVER_URL:-}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-}"

for i in 1 2 3; do
    if git -C "$WT" push origin "HEAD:refs/heads/$BRANCH"; then
        echo "published $RUN_DIR on $BRANCH (try $i)"
        exit 0
    fi
    echo "push rejected (try $i/3): replaying the commit on the new tip"
    sleep $((i * 3))
    git -C "$WT" fetch --depth=1 origin "$BRANCH"
    git -C "$WT" rebase --onto FETCH_HEAD HEAD~1
done
echo "could not publish results after 3 tries"
exit 1
