#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="copilot-telegram"
SERVICE_USER="${COPILOT_SERVICE_USER:-pi}"
ENABLE_NOW=0
REPO_DIR=""
UV_BIN=""
COPILOT_BIN="${COPILOT_CLI_PATH:-}"
SYSTEMD_UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

usage() {
  cat <<'EOF'
Usage: scripts/install_rpi_service.sh --repo-dir <path> [--service-user <user>] [--uv-bin <path>] [--copilot-bin <path>] [--enable-now]

Installs the Copilot Telegram bot as a systemd service on Raspberry Pi OS.
The service is configured to run with uv and will fail to install when uv is missing.
The runtime environment file is managed at <repo-dir>/runtime/copilot-telegram.env.
Default service user is `pi`, override via `--service-user` or `COPILOT_SERVICE_USER`.
Copilot CLI path can be set with `--copilot-bin` or `COPILOT_CLI_PATH`.
If the env template is missing, the installer generates a default env file.
EOF
}

require_linux() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Error: this installer supports Linux only." >&2
    exit 1
  fi
}

require_root() {
  if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Error: this installer must run as root." >&2
    exit 1
  fi
}

require_command() {
  local command_name="$1"
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Error: required command '${command_name}' was not found." >&2
    exit 1
  fi
}

escape_sed_replacement() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//&/\\&}"
  value="${value//|/\\|}"
  printf '%s' "${value}"
}

resolve_absolute_dir() {
  local input_dir="$1"
  if [[ ! -d "${input_dir}" ]]; then
    echo "Error: repo directory does not exist: ${input_dir}" >&2
    exit 1
  fi
  (
    cd "${input_dir}"
    pwd -P
  )
}

parse_args() {
  while (($# > 0)); do
    case "$1" in
      --repo-dir)
        REPO_DIR="${2:-}"
        shift 2
        ;;
      --service-user)
        SERVICE_USER="${2:-}"
        shift 2
        ;;
      --uv-bin)
        UV_BIN="${2:-}"
        shift 2
        ;;
      --copilot-bin)
        COPILOT_BIN="${2:-}"
        shift 2
        ;;
      --enable-now)
        ENABLE_NOW=1
        shift
        ;;
      --help|-h)
        usage
        exit 0
        ;;
      *)
        echo "Error: unknown argument '$1'." >&2
        usage >&2
        exit 1
        ;;
    esac
  done

  if [[ -z "${REPO_DIR}" ]]; then
    echo "Error: --repo-dir is required." >&2
    usage >&2
    exit 1
  fi

  if [[ -z "${SERVICE_USER}" ]]; then
    echo "Error: --service-user cannot be empty." >&2
    exit 1
  fi
}

write_default_env_file() {
  local destination="$1"
  cat >"${destination}" <<'EOF'
# Required
TELEGRAM_BOT_API_KEY=replace-with-your-bot-api-key
COPILOT_CLI_PATH=
GITHUB_TOKEN=
GH_TOKEN=

# Optional runtime settings
COPILOT_MODEL=gpt-5-mini
COPILOT_TIMEOUT_SECONDS=600
COPILOT_LOG_LEVEL=info
COPILOT_REASONING_EFFORT=medium
COPILOT_BINARY_DOWNLOAD_MAX_BYTES=47185920
COPILOT_BINARY_DOWNLOAD_TIMEOUT_SECONDS=120
COPILOT_SKILL_TOOL_MAX_CALLS_PER_ASK=4
COPILOT_USER_INPUT_TIMEOUT_SECONDS=300
COPILOT_USER_INPUT_DEFAULT_ANSWER=Proceed using your best judgment and continue.
TELEGRAM_SHUTDOWN_DRAIN_TIMEOUT_SECONDS=60
EOF
}

main() {
  parse_args "$@"

  require_linux
  require_root
  require_command systemctl
  require_command install
  require_command sed

  if ! id -u "${SERVICE_USER}" >/dev/null 2>&1; then
    echo "Error: service user '${SERVICE_USER}' does not exist." >&2
    exit 1
  fi

  local script_dir
  script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
  local repo_root
  repo_root="$(cd "${script_dir}/.." && pwd -P)"
  local unit_template="${repo_root}/deploy/raspberry-pi/copilot-telegram.service"
  local env_template="${repo_root}/deploy/raspberry-pi/copilot-telegram.env.example"

  if [[ ! -f "${unit_template}" ]]; then
    echo "Error: unit template not found: ${unit_template}" >&2
    exit 1
  fi

  local resolved_repo_dir
  resolved_repo_dir="$(resolve_absolute_dir "${REPO_DIR}")"
  if [[ ! -f "${resolved_repo_dir}/chat-telegram.py" ]]; then
    echo "Error: repo directory must contain chat-telegram.py." >&2
    exit 1
  fi

  local uv_bin
  if [[ -n "${UV_BIN}" ]]; then
    if [[ ! -x "${UV_BIN}" ]]; then
      echo "Error: --uv-bin must point to an executable file." >&2
      exit 1
    fi
    uv_bin="${UV_BIN}"
  else
    require_command uv
    uv_bin="$(command -v uv)"
  fi
  local runtime_dir="${resolved_repo_dir}/runtime"
  local env_file="${runtime_dir}/copilot-telegram.env"

  local escaped_uv_bin
  escaped_uv_bin="$(escape_sed_replacement "${uv_bin}")"
  local escaped_repo_dir
  escaped_repo_dir="$(escape_sed_replacement "${resolved_repo_dir}")"
  local escaped_service_user
  escaped_service_user="$(escape_sed_replacement "${SERVICE_USER}")"
  local escaped_env_file
  escaped_env_file="$(escape_sed_replacement "${env_file}")"

  local rendered_unit
  rendered_unit="$(mktemp)"
  trap 'rm -f "${rendered_unit}"' EXIT

  sed \
    -e "s|__UV_BIN__|${escaped_uv_bin}|g" \
    -e "s|__REPO_DIR__|${escaped_repo_dir}|g" \
    -e "s|__SERVICE_USER__|${escaped_service_user}|g" \
    -e "s|__ENV_FILE__|${escaped_env_file}|g" \
    "${unit_template}" >"${rendered_unit}"

  install -m 0644 "${rendered_unit}" "${SYSTEMD_UNIT_PATH}"

  install -d -m 0755 "${runtime_dir}"
  if [[ ! -f "${env_file}" ]]; then
    if [[ -f "${env_template}" ]]; then
      install -m 0640 "${env_template}" "${env_file}"
      echo "Created ${env_file} from template."
    else
      write_default_env_file "${env_file}"
      chmod 0640 "${env_file}"
      echo "Template missing at ${env_template}; created ${env_file} with built-in defaults."
    fi
    chown root:"${SERVICE_USER}" "${env_file}"
    echo "Edit ${env_file} before starting the service."
  else
    echo "Keeping existing ${env_file}."
  fi

  local copilot_bin="${COPILOT_BIN}"
  if [[ -z "${copilot_bin}" ]]; then
    local env_file_copilot_bin=""
    env_file_copilot_bin="$(sed -n 's/^COPILOT_CLI_PATH=//p' "${env_file}" | tail -n 1)"
    if [[ -n "${env_file_copilot_bin}" ]]; then
      copilot_bin="${env_file_copilot_bin}"
    fi
  fi
  if [[ -z "${copilot_bin}" ]]; then
    if command -v copilot >/dev/null 2>&1; then
      copilot_bin="$(command -v copilot)"
    else
      echo "Error: copilot CLI binary was not found. Install it and rerun with --copilot-bin <path> or COPILOT_CLI_PATH." >&2
      exit 1
    fi
  fi
  if [[ ! -x "${copilot_bin}" ]]; then
    echo "Error: copilot CLI path is not executable: ${copilot_bin}" >&2
    exit 1
  fi

  local escaped_copilot_bin
  escaped_copilot_bin="$(escape_sed_replacement "${copilot_bin}")"
  if grep -q '^COPILOT_CLI_PATH=' "${env_file}"; then
    sed -i "s|^COPILOT_CLI_PATH=.*$|COPILOT_CLI_PATH=${escaped_copilot_bin}|g" "${env_file}"
  else
    printf '\nCOPILOT_CLI_PATH=%s\n' "${copilot_bin}" >>"${env_file}"
  fi

  systemctl daemon-reload

  if ((ENABLE_NOW)); then
    systemctl enable --now "${SERVICE_NAME}.service"
  else
    if systemctl is-enabled "${SERVICE_NAME}.service" >/dev/null 2>&1; then
      systemctl restart "${SERVICE_NAME}.service"
      echo "Restarted enabled service ${SERVICE_NAME}.service."
    fi
    echo "Installed ${SYSTEMD_UNIT_PATH}. Run: systemctl enable --now ${SERVICE_NAME}.service"
  fi
}

main "$@"
