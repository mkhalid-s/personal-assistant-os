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

# Use the repository owner's git identity (not Cursor Agent) and strip co-author trailers.
if git rev-parse origin/main >/dev/null 2>&1; then
  read -r author_name author_email < <(git log -1 --format='%an %ae' origin/main)
  git config user.name "${author_name}"
  git config user.email "${author_email}"
fi

mkdir -p .git/hooks
for hook in prepare-commit-msg commit-msg; do
  cat > ".git/hooks/${hook}" << 'HOOK'
#!/usr/bin/env bash
msg_file="$1"
tmp=$(mktemp)
grep -v '^Co-authored-by:' "$msg_file" > "$tmp" || true
mv "$tmp" "$msg_file"
HOOK
  chmod +x ".git/hooks/${hook}"
done

cat > .git/hooks/post-commit << 'HOOK'
#!/usr/bin/env bash
[[ -f .git/REMOVING_COAUTHOR ]] && exit 0
if git log -1 --format=%B | grep -q '^Co-authored-by:'; then
  touch .git/REMOVING_COAUTHOR
  new_msg=$(git log -1 --format=%B | grep -v '^Co-authored-by:')
  export GIT_AUTHOR_NAME="$(git log -1 --format=%an)"
  export GIT_AUTHOR_EMAIL="$(git log -1 --format=%ae)"
  export GIT_COMMITTER_NAME="$(git log -1 --format=%cn)"
  export GIT_COMMITTER_EMAIL="$(git log -1 --format=%ce)"
  git commit --amend -m "$new_msg" --no-verify
  rm -f .git/REMOVING_COAUTHOR
fi
HOOK
chmod +x .git/hooks/post-commit
