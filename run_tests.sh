#!/usr/bin/env bash
# Run every test module and report only what CHANGED against the baseline.
#
# WHY A BASELINE FILE EXISTS AT ALL. 19 modules in this repo fail on a clean
# checkout of main - network-dependent, environment-dependent, or genuinely
# broken and not yet fixed. Without a recorded list, "the suite is red" is
# true on every run and therefore means nothing, and a real regression hides
# among 19 pre-existing failures. This makes the baseline a checked-in fact
# instead of tribal knowledge, so a NEW failure is unmissable.
#
# WHY EACH MODULE IS RUN AS WRITTEN, not under `python -m unittest`. These
# modules are not uniformly unittest - many are plain scripts with their own
# ok()/FAIL accounting and an explicit sys.exit. Running them through the
# unittest loader collects a different set and changes the failure count,
# which has produced phantom "new failures" before. `python3 -B <file>` is
# the invocation their exit codes are written for.
#
#   ./run_tests.sh            run everything, compare to baseline
#   ./run_tests.sh --update   rewrite the baseline from this run
#   ./run_tests.sh pattern    only modules matching a substring
set -uo pipefail
cd "$(dirname "$0")"

BASELINE=test_baseline.txt
UPDATE=0
PATTERN=""
for a in "$@"; do
  case "$a" in
    --update) UPDATE=1 ;;
    -*) echo "unknown flag: $a" >&2; exit 2 ;;
    *) PATTERN="$a" ;;
  esac
done

mapfile -t MODULES < <(ls -1 test_*.py 2>/dev/null | { [ -n "$PATTERN" ] && grep -- "$PATTERN" || cat; })
[ ${#MODULES[@]} -eq 0 ] && { echo "no test modules matched"; exit 2; }

# __pycache__ is cleared first: a stale .pyc from a module that was edited
# between runs has silently masked a real failure here before.
rm -rf __pycache__ 2>/dev/null

NOW=$(mktemp); PASSED=0; FAILED=0
printf 'running %d module(s)\n\n' "${#MODULES[@]}"
for f in "${MODULES[@]}"; do
  if timeout 180 python3 -B "$f" >/dev/null 2>&1; then
    PASSED=$((PASSED+1)); printf '.'
  else
    FAILED=$((FAILED+1)); printf 'F'; echo "$f" >> "$NOW"
  fi
done
sort -o "$NOW" "$NOW" 2>/dev/null || : > "$NOW"
printf '\n\n%d passed, %d failed, %d total\n' "$PASSED" "$FAILED" "${#MODULES[@]}"

if [ "$UPDATE" = "1" ]; then
  cp "$NOW" "$BASELINE"
  echo "baseline rewritten: $(wc -l < "$BASELINE" | tr -d ' ') known failure(s) in $BASELINE"
  exit 0
fi

if [ ! -f "$BASELINE" ]; then
  echo "no $BASELINE - run ./run_tests.sh --update once to record the current state"
  exit 1
fi
[ -n "$PATTERN" ] && { echo "(filtered run - baseline comparison skipped)"; exit 0; }

NEW=$(comm -23 "$NOW" "$BASELINE")
FIXED=$(comm -13 "$NOW" "$BASELINE")
rc=0
if [ -n "$NEW" ]; then
  echo; echo "NEW FAILURES - these were passing on the baseline:"
  echo "$NEW" | sed 's/^/  /'
  rc=1
fi
if [ -n "$FIXED" ]; then
  echo; echo "NEWLY PASSING - fix the baseline if this is intended:"
  echo "$FIXED" | sed 's/^/  /'
  echo "  (./run_tests.sh --update)"
fi
[ -z "$NEW" ] && [ -z "$FIXED" ] && echo "identical to the baseline: no new failures, none fixed"
rm -f "$NOW"
exit $rc
