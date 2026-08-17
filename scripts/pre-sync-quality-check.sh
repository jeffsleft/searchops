#!/usr/bin/env bash
#
# pre-sync-quality-check.sh — deterministic quality gate run before every
# public-repo sync.  No LLM calls.  Exit 0 = clean; non-zero = hard problem.
#
# Checks:
#   1. Scoring-range consistency — numeric claims in README/docs must be
#      plausible against app/scoring/engine.py and candidate_profile.yaml.
#   2. File-path validity — every path mentioned in README/docs that we can
#      cheaply pattern-match must exist in the repo.
#   3. Recent-commit flag — warn if any of the last 10 commits touched files
#      that are referenced in README/docs (potential stale-claim risk).
#
# This script is intentionally conservative about what it flags as a HARD
# failure vs. a warning.  Hard failures block the sync.  Warnings are printed
# but do not set fail=1.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

fail=0

warn()  { echo "  ⚠  $*"; }
error() { echo "  ✗  $*"; fail=1; }
ok()    { echo "  ✓  $*"; }

echo "==> Pre-sync quality check"

# ----------------------------------------------------------------------------
# 1. Scoring-range consistency
# ----------------------------------------------------------------------------
echo ""
echo "--- [1] Scoring ranges ---"

# Read ground-truth values from candidate_profile.yaml (or the example).
PROFILE="candidate_profile.yaml"
[[ -f "$PROFILE" ]] || PROFILE="candidate_profile.example.yaml"

if [[ ! -f "$PROFILE" ]]; then
  error "No candidate_profile.yaml or candidate_profile.example.yaml found"
else
  # Extract only the value after the colon — grep -oE on the whole line would
  # also match stray digits in the key name itself (e.g. the "4" in
  # "max_l4_positive"), corrupting the result.
  # [[:space:]] (POSIX) rather than \s — BSD sed (macOS, used for local
  # dry-runs) doesn't support \s as a GNU extension the way GNU sed (CI,
  # ubuntu-latest) does; it would silently no-op and leave a leading space
  # that breaks the anchored digit match below.
  BASE_SCORE=$(grep -E '^[[:space:]]*base_score:' "$PROFILE" | head -1 | sed -E 's/^[^:]*:[[:space:]]*//' | grep -oE '^[0-9]+(\.[0-9]+)?')
  MAX_L4=$(grep -E '^[[:space:]]*max_l4_positive:' "$PROFILE" | head -1 | sed -E 's/^[^:]*:[[:space:]]*//' | grep -oE '^[0-9]+(\.[0-9]+)?')
  GATE_THRESHOLD=$(grep -E '^[[:space:]]*top_band_gate_threshold:' "$PROFILE" | head -1 | sed -E 's/^[^:]*:[[:space:]]*//' | grep -oE '^[0-9]+(\.[0-9]+)?')
  COMP_MIN=$(grep -E '^[[:space:]]*base_min:' "$PROFILE" | head -1 | sed -E 's/^[^:]*:[[:space:]]*//' | grep -oE '^[0-9]+')

  ok "Profile: base_score=$BASE_SCORE  max_l4_positive=$MAX_L4  gate_threshold=$GATE_THRESHOLD  comp_min=$COMP_MIN"

  # README currently states "an 8+ is rare" and "cap out at 6.0" and "above 7.0".
  # Soft-check: base_score must be < gate_threshold, gate_threshold must be >= 7.0.
  if python3 -c "
import sys
base  = float('${BASE_SCORE:-0}')
gate  = float('${GATE_THRESHOLD:-0}')
l4max = float('${MAX_L4:-0}')
errors = []
if base <= 0 or base >= 10:
    errors.append(f'base_score {base} out of 0-10 range')
if gate < 7.0:
    errors.append(f'top_band_gate_threshold {gate} < 7.0 but README says 8+ is rare')
if l4max <= 0 or l4max > 5:
    errors.append(f'max_l4_positive {l4max} looks implausible (expected 0-5)')
if base + l4max >= 10:
    errors.append(f'base_score + max_l4_positive ({base}+{l4max}) >= 10 — scores would be uncapped')
if errors:
    print('SCORING_ERRORS: ' + ' | '.join(errors))
    sys.exit(1)
" 2>&1; then
    ok "Scoring parameter ranges are self-consistent"
  else
    error "Scoring parameter ranges are inconsistent — see above"
  fi
fi

# Check engine.py surface-score cap.  The code should contain a comment or
# literal referencing the 6.0 / 10.0 scale somewhere.
if grep -qE '6\.0|score.*cap|max.*surface' app/scoring/engine.py 2>/dev/null; then
  ok "engine.py contains a surface-score cap reference"
else
  warn "engine.py may be missing a 6.0 surface-score cap reference (README claims it)"
fi

# ----------------------------------------------------------------------------
# 2. File-path validity
# ----------------------------------------------------------------------------
echo ""
echo "--- [2] File-path references in README and docs ---"

# Extract path-like tokens that look like repo files from README + case study.
# Pattern: word characters and slashes that look like relative repo paths,
# optionally in backticks or parentheses.  We look for things like:
#   app/scoring/engine.py   scripts/sync-public.sh   docs/hosting.md   etc.
DOCS_TO_SCAN=("README.md")
[[ -f "docs/case-study.md" ]] && DOCS_TO_SCAN+=("docs/case-study.md")

PATH_FAIL=0
while IFS= read -r candidate_path; do
  # Skip things that look like URLs or are too short to be meaningful.
  [[ "$candidate_path" =~ ^https?:// ]] && continue
  [[ ${#candidate_path} -lt 5 ]] && continue
  [[ -e "$REPO_ROOT/$candidate_path" ]] && continue
  # A path can legitimately not exist on disk if it's gitignored personal
  # data the README references as setup instructions (e.g. "cp
  # hunt_targets.example.yaml hunt_targets.yaml", "your corpus (gitignored)").
  # Only flag paths that are neither present NOR expected to be absent.
  if git -C "$REPO_ROOT" check-ignore -q "$candidate_path" 2>/dev/null; then
    continue
  fi
  error "Referenced path not found: $candidate_path"
  PATH_FAIL=1
done < <(
  grep -ohE '[a-zA-Z_][a-zA-Z0-9_./\-]+\.[a-z]{2,5}' "${DOCS_TO_SCAN[@]}" 2>/dev/null \
    | grep -E '^(app|scripts|docs|tests|data|seed_data)/' \
    | sort -u
)

if [[ "$PATH_FAIL" -eq 0 ]]; then
  ok "All repo-relative paths referenced in docs exist"
fi

# ----------------------------------------------------------------------------
# 3. Recent-commit flag
# ----------------------------------------------------------------------------
echo ""
echo "--- [3] Recent-commit / stale-claim check ---"

# Files touched in the last 10 commits.
RECENT_FILES=$(git log --name-only --pretty=format: -10 HEAD | grep -v '^$' | sort -u)

# Key scoring/config files whose changes most likely invalidate public claims.
SENTINEL_FILES=(
  "app/scoring/engine.py"
  "app/scoring/prompts.py"
  "candidate_profile.yaml"
  "candidate_profile.example.yaml"
)

STALE_RISK=0
for f in "${SENTINEL_FILES[@]}"; do
  if echo "$RECENT_FILES" | grep -qF "$f"; then
    warn "Recently modified: $f — verify README/docs claims are still accurate"
    STALE_RISK=1
  fi
done

if [[ "$STALE_RISK" -eq 0 ]]; then
  ok "No sentinel files changed in last 10 commits"
else
  echo "  (Warnings above are advisory — they do not block the sync)"
fi

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
echo ""
if [[ "$fail" -ne 0 ]]; then
  echo "==> QUALITY GATE FAILED — sync aborted. Fix the issues above and re-run."
  exit 1
fi
echo "==> Quality gate passed."
exit 0
