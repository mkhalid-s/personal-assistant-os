#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e .

mkdir -p data
if [[ ! -f data/.env.myos ]]; then
  cp .env.example data/.env.myos
fi

mkdir -p "${HOME}/.local/bin"
ln -sf "$(pwd)/.venv/bin/myos" "${HOME}/.local/bin/myos"

# Set git identity from repo owner only if not already configured locally.
# Skips human developers who have their own identity; corrects cloud agent
# environments (e.g. Cursor) where no local identity is set.
if [[ -z "$(git config --local user.name 2>/dev/null)" ]] || \
   [[ -z "$(git config --local user.email 2>/dev/null)" ]]; then
  if ! git rev-parse --verify origin/main >/dev/null 2>&1; then
    git fetch --depth=1 origin refs/heads/main:refs/remotes/origin/main
  fi
  author_name="$(git log -1 --format='%an' origin/main)"
  author_email="$(git log -1 --format='%ae' origin/main)"
  git config --local user.name "${author_name}"
  git config --local user.email "${author_email}"
  echo "Set git identity from last commit on origin/main: ${author_name} <${author_email}>"
fi

mkdir -p .git/hooks
rm -f .git/hooks/post-commit .git/REMOVING_COAUTHOR

# Install co-author stripping hooks. Idempotent: skips if the hook already
# contains the pattern. If an existing hook is found without the pattern it
# is preserved and the stripping is prepended — so prior hooks still run.
_coauthor_marker='grep -vi .Co-authored-'
for hook in prepare-commit-msg commit-msg; do
  hook_path=".git/hooks/${hook}"
  if [[ -f "${hook_path}" ]] && grep -q "${_coauthor_marker}" "${hook_path}" 2>/dev/null; then
    echo "Hook ${hook} already strips co-author lines — skipping"
    continue
  fi
  if [[ -f "${hook_path}" ]]; then
    echo "Existing ${hook} hook found — prepending co-author stripping"
    existing_body="$(cat "${hook_path}")"
    {
      printf '#!/usr/bin/env bash\n'
      printf '# co-author stripping (prepended by cloud-agent-install.sh)\n'
      printf 'msg_file="$1"\n'
      printf 'tmp=$(mktemp)\n'
      printf "grep -vi '^Co-authored-''by:' \"\$msg_file\" > \"\$tmp\" || true\n"
      printf 'mv "$tmp" "$msg_file"\n'
      printf '\n# original hook below:\n'
      printf '%s\n' "${existing_body}"
    } > "${hook_path}"
  else
    cat > "${hook_path}" << 'HOOK'
#!/usr/bin/env bash
msg_file="$1"
tmp=$(mktemp)
grep -vi '^Co-authored-''by:' "$msg_file" > "$tmp" || true
mv "$tmp" "$msg_file"
HOOK
  fi
  chmod +x "${hook_path}"
done
