#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────
# Install git hooks for AI-Trading-Bot
# ──────────────────────────────────────────────────────────────
# Usage:  bash scripts/install-hooks.sh
# ──────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOOKS_DIR="${REPO_ROOT}/.git/hooks"

echo "Installing git hooks..."

# Install pre-push hook
cp "${REPO_ROOT}/scripts/pre-push" "${HOOKS_DIR}/pre-push"
chmod +x "${HOOKS_DIR}/pre-push"

echo "Installed: pre-push (runs syntax check + critical tests before push)"
echo ""
echo "To skip hooks in an emergency:  git push --no-verify"
echo "Done."
