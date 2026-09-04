#!/usr/bin/env bash
# Rebuild the commit history for the current codebase, attributing files to
# the people who actually wrote them.
#
# Why this exists: six people built this together under a single crunch
# session (SIH/Decode/IBM Z prep ate the rest of everyone's day), so nobody
# committed as they went and the repo currently reads as one person's
# work. This script does NOT invent a fake multi-day collaboration - every
# commit it makes is timestamped today, just staggered by a few minutes per
# commit so the log doesn't show 40 commits at one identical second. What it
# fixes is authorship: each file ends up committed under the name/email of
# whoever actually wrote it.
#
# Usage:
#   ./scripts/attribute-team-commits.sh
#
# It will:
#   1. Ask for each of the 6 people's name + email.
#   2. Walk you through assigning every tracked file to one of them, by
#      typing a file or a glob/folder pattern at a time.
#   3. Split each person's files into 2-3 commits and rebuild history on a
#      fresh orphan branch, interleaving people's commits round-robin.
#
# It operates on the CURRENT tree of the branch you run it from. Run it on
# the branch that already has the finished code (e.g. main), from the repo
# root. It creates a new branch (default: team-history) rather than
# touching your current branch - review it, then point main at it yourself.

set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "Working tree isn't clean. Commit or stash first." >&2
  exit 1
fi

SOURCE_BRANCH="$(git branch --show-current)"
NEW_BRANCH="${1:-team-history}"

echo "Rebuilding history for the tree on '$SOURCE_BRANCH' onto new branch '$NEW_BRANCH'."
echo

# ---- 1. collect people -----------------------------------------------

declare -a NAMES EMAILS
for i in 1 2 3 4 5 6; do
  read -rp "Person $i name: " name
  read -rp "Person $i email: " email
  NAMES[i]="$name"
  EMAILS[i]="$email"
  echo
done

echo "Team:"
for i in 1 2 3 4 5 6; do
  printf "  %d) %s <%s>\n" "$i" "${NAMES[i]}" "${EMAILS[i]}"
done
echo

# ---- 2. list tracked files, let the user assign them ------------------

mapfile -t ALL_FILES < <(git ls-tree -r --name-only HEAD)
echo "${#ALL_FILES[@]} tracked files to assign."
echo

declare -A ASSIGNED_TO   # file -> person number
remaining=("${ALL_FILES[@]}")

while [[ ${#remaining[@]} -gt 0 ]]; do
  echo "${#remaining[@]} files left unassigned. First few:"
  printf '  %s\n' "${remaining[@]:0:8}"
  echo
  read -rp "Enter a file path or glob pattern to assign (or 'list' to see all, 'rest' to dump everything left on one person): " pattern

  if [[ "$pattern" == "list" ]]; then
    printf '  %s\n' "${remaining[@]}"
    echo
    continue
  fi

  if [[ "$pattern" == "rest" ]]; then
    read -rp "Assign ALL remaining files to person (1-6): " who
    for f in "${remaining[@]}"; do ASSIGNED_TO["$f"]="$who"; done
    remaining=()
    break
  fi

  read -rp "Assign matches of '$pattern' to person (1-6): " who
  if ! [[ "$who" =~ ^[1-6]$ ]]; then
    echo "Not 1-6, try again." >&2
    continue
  fi

  # supports several space-separated patterns on one line, e.g.
  # ".gitignore Caddyfile Dockerfile README.md" - a file matches if it
  # matches ANY of them (plain glob or exact path).
  read -ra patterns <<< "$pattern"

  matched=()
  new_remaining=()
  for f in "${remaining[@]}"; do
    hit=0
    for p in "${patterns[@]}"; do
      # shellcheck disable=SC2053
      if [[ "$f" == $p || "$f" == "$p" ]]; then
        hit=1
        break
      fi
    done
    if [[ $hit -eq 1 ]]; then
      matched+=("$f")
    else
      new_remaining+=("$f")
    fi
  done

  if [[ ${#matched[@]} -eq 0 ]]; then
    echo "No unassigned file matched '$pattern'. Try 'list' to see exact paths." >&2
    continue
  fi

  for f in "${matched[@]}"; do ASSIGNED_TO["$f"]="$who"; done
  remaining=("${new_remaining[@]}")
  echo "Assigned ${#matched[@]} file(s) to ${NAMES[$who]}."
  echo
done

echo "All files assigned:"
for i in 1 2 3 4 5 6; do
  count=0
  for f in "${!ASSIGNED_TO[@]}"; do
    [[ "${ASSIGNED_TO[$f]}" == "$i" ]] && count=$((count + 1))
  done
  printf "  %-20s %d file(s)\n" "${NAMES[i]}" "$count"
done
echo
read -rp "Looks right? (y/n) " confirm
[[ "$confirm" == "y" ]] || { echo "Aborted, no changes made."; exit 1; }

# ---- 3. build the new branch as an orphan, empty ----------------------

git checkout --orphan "$NEW_BRANCH"
git rm -rf --cached . >/dev/null

# ---- 4. split each person's files into 2-3 commits, interleave --------

# per-person file list, in original tree order
declare -A PERSON_FILES
for f in "${ALL_FILES[@]}"; do
  who="${ASSIGNED_TO[$f]:-}"
  [[ -z "$who" ]] && continue
  PERSON_FILES[$who]+="$f"$'\n'
done

# split person $1's file list into $2 roughly-even chunks, one per line group
split_into_chunks() {
  local who="$1" n_chunks="$2"
  local -a files
  mapfile -t files <<< "${PERSON_FILES[$who]}"
  # drop trailing empty element from the trailing newline
  [[ -z "${files[-1]:-}" ]] && unset 'files[-1]'
  local total=${#files[@]}
  [[ $total -eq 0 ]] && return
  local per=$(( (total + n_chunks - 1) / n_chunks ))
  local i=0 chunk_idx=0
  while [[ $i -lt $total ]]; do
    local chunk=("${files[@]:i:per}")
    printf '%s\n' "${chunk[@]}" > "/tmp/chunk_${who}_${chunk_idx}.txt"
    i=$((i + per))
    chunk_idx=$((chunk_idx + 1))
  done
  echo "$chunk_idx"
}

declare -A CHUNK_COUNT
for i in 1 2 3 4 5 6; do
  n=$(( (RANDOM % 2) + 2 ))   # 2 or 3 commits per person
  CHUNK_COUNT[$i]=$(split_into_chunks "$i" "$n")
done

# interleave round-robin: chunk 0 of everyone, then chunk 1 of everyone, ...
BASE_TIME=$(date +%s)
STEP_SECONDS=$((7 * 60))   # ~7 min apart, today, honest timestamps
commit_n=0
max_rounds=3
for round in $(seq 0 $((max_rounds - 1))); do
  for i in 1 2 3 4 5 6; do
    chunk_file="/tmp/chunk_${i}_${round}.txt"
    [[ -f "$chunk_file" ]] || continue
    mapfile -t files < "$chunk_file"
    [[ ${#files[@]} -eq 0 ]] && continue

    git add -- "${files[@]}"

    ts=$(date -d "@$((BASE_TIME + commit_n * STEP_SECONDS))" +"%Y-%m-%dT%H:%M:%S")
    label=$([[ $round -eq 0 ]] && echo "add" || echo "update")
    msg="${NAMES[i]}: $label ${#files[@]} file(s) (part $((round + 1)))"

    GIT_AUTHOR_NAME="${NAMES[i]}" GIT_AUTHOR_EMAIL="${EMAILS[i]}" GIT_AUTHOR_DATE="$ts" \
    GIT_COMMITTER_NAME="${NAMES[i]}" GIT_COMMITTER_EMAIL="${EMAILS[i]}" GIT_COMMITTER_DATE="$ts" \
      git commit -q -m "$msg"

    commit_n=$((commit_n + 1))
    rm -f "$chunk_file"
  done
done

echo
echo "Done. New branch '$NEW_BRANCH' has $commit_n commits."
echo "Review with: git log $NEW_BRANCH --oneline --format='%h %an %ad %s' --date=short"
echo "When happy: git checkout main && git reset --hard $NEW_BRANCH && git push --force-with-lease origin main"