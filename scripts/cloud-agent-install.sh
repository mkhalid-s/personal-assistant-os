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

# Use the repository owner's git identity (not Cursor Agent).
if ! git rev-parse --verify origin/main >/dev/null 2>&1; then
  git fetch --depth=1 origin refs/heads/main:refs/remotes/origin/main
fi
author_name="$(git log -1 --format='%an' origin/main)"
author_email="$(git log -1 --format='%ae' origin/main)"
git config --local user.name "${author_name}"
git config --local user.email "${author_email}"

mkdir -p .git/hooks
rm -f .git/hooks/post-commit .git/REMOVING_COAUTHOR
for hook in prepare-commit-msg commit-msg; do
  cat > ".git/hooks/${hook}" << 'HOOK'
#!/usr/bin/env bash
msg_file="$1"
tmp=$(mktemp)
grep -vi '^Co-authored-''by:' "$msg_file" > "$tmp" || true
mv "$tmp" "$msg_file"
HOOK
  chmod +x ".git/hooks/${hook}"
done
