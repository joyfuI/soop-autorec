#!/usr/bin/env bash
set -u

BOOTSTRAP_REPO_URL="${BOOTSTRAP_REPO_URL:-https://github.com/joyfuI/soop-autorec.git}"
BOOTSTRAP_REPO_BRANCH="${BOOTSTRAP_REPO_BRANCH:-main}"
APP_DIR="/workspace"

log() {
  printf '[bootstrap] %s\n' "$*"
}

ensure_repo() {
  mkdir -p "${APP_DIR}"

  if [[ -d "${APP_DIR}/.git" ]]; then
    return 0
  fi

  if [[ -f "${APP_DIR}/pyproject.toml" ]]; then
    log "warning: ${APP_DIR} has project files but is not a git repository. Using existing files."
    return 0
  fi

  log "bootstrapping repository: ${BOOTSTRAP_REPO_URL} (${BOOTSTRAP_REPO_BRANCH})"
  if git -C "${APP_DIR}" init -q \
    && git -C "${APP_DIR}" remote add origin "${BOOTSTRAP_REPO_URL}" \
    && git -C "${APP_DIR}" fetch --depth 1 origin "${BOOTSTRAP_REPO_BRANCH}" \
    && git -C "${APP_DIR}" checkout -B "${BOOTSTRAP_REPO_BRANCH}" FETCH_HEAD; then
    log "bootstrap clone completed."
    return 0
  fi

  rm -rf "${APP_DIR}/.git" 2>/dev/null || true
  log "error: initial git clone failed."
  return 1
}

update_repo() {
  if [[ ! -d "${APP_DIR}/.git" ]]; then
    log "warning: ${APP_DIR} is not a git repository. Skipping git pull."
    return 0
  fi

  log "updating repository (git pull --ff-only)..."
  if git -C "${APP_DIR}" pull --ff-only origin "${BOOTSTRAP_REPO_BRANCH}"; then
    log "git pull completed."
  else
    log "warning: git pull failed. Continuing with existing code."
  fi
}

sync_dependencies() {
  if [[ ! -f "${APP_DIR}/pyproject.toml" ]]; then
    log "warning: pyproject.toml not found. Skipping dependency sync."
    return 0
  fi

  log "syncing Python dependencies (uv sync)..."
  if (cd "${APP_DIR}" && uv sync); then
    log "uv sync completed."
  else
    log "warning: uv sync failed. Continuing with existing environment."
  fi
}

run_app() {
  if [[ ! -d "${APP_DIR}/.venv" ]]; then
    log "error: .venv not found. Cannot start app without dependencies."
    return 1
  fi

  if [[ ! -f "${APP_DIR}/app/main.py" ]]; then
    log "error: app source not found at ${APP_DIR}/app/main.py"
    return 1
  fi

  log "starting application..."
  cd "${APP_DIR}"
  exec uv run --no-sync python -m app.main
}

main() {
  ensure_repo || exit 1
  update_repo
  sync_dependencies
  run_app
}

main "$@"
