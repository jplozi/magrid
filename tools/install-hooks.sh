#!/bin/sh
# Install the post-commit hook of this checkout: after each commit, push main
# to gitlab.inria.fr and refresh the GitHub mirror (tools/mirror-github.py),
# in the background, with a log in .git/mirror.log.
set -eu
cd "$(git rev-parse --show-toplevel)"
cat > .git/hooks/post-commit <<'EOF'
#!/bin/sh
# installed by tools/install-hooks.sh
[ "$(git rev-parse --abbrev-ref HEAD)" = main ] || exit 0
(
  echo "== $(date '+%F %T') $(git rev-parse --short HEAD)"
  git push --quiet origin main && tools/mirror-github.py
) >> .git/mirror.log 2>&1 &
EOF
chmod +x .git/hooks/post-commit
echo "post-commit hook installed: commits on main are pushed to origin and mirrored to GitHub"
