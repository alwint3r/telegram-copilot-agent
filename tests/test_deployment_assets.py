from __future__ import annotations

from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_TEMPLATE = REPO_ROOT / "deploy" / "raspberry-pi" / "copilot-telegram.service"
INSTALL_SCRIPT = REPO_ROOT / "scripts" / "install_rpi_service.sh"
ENV_TEMPLATE = REPO_ROOT / "deploy" / "raspberry-pi" / "copilot-telegram.env.example"


class DeploymentAssetTests(unittest.TestCase):
    def test_service_template_contains_required_directives(self) -> None:
        content = SERVICE_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("After=network-online.target", content)
        self.assertIn("Wants=network-online.target", content)
        self.assertIn("EnvironmentFile=__ENV_FILE__", content)
        self.assertIn("ExecStart=__UV_BIN__ run chat-telegram.py --working-directory __REPO_DIR__", content)
        self.assertIn("Restart=always", content)
        self.assertIn("WantedBy=multi-user.target", content)

    def test_service_template_is_uv_only(self) -> None:
        content = SERVICE_TEMPLATE.read_text(encoding="utf-8").lower()
        self.assertIn("__uv_bin__ run", content)
        self.assertNotIn("python3", content)
        self.assertNotIn("python ", content)

    def test_install_script_enforces_guardrails(self) -> None:
        content = INSTALL_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("set -euo pipefail", content)
        self.assertIn("uname -s", content)
        self.assertIn("EUID", content)
        self.assertIn("require_command uv", content)
        self.assertIn("command -v uv", content)
        self.assertIn("--uv-bin", content)
        self.assertIn("--copilot-bin", content)
        self.assertIn("COPILOT_CLI_PATH", content)
        self.assertIn("command -v copilot", content)
        self.assertIn("copilot CLI binary was not found", content)
        self.assertIn("--service-user", content)
        self.assertIn("COPILOT_SERVICE_USER", content)
        self.assertIn("write_default_env_file", content)
        self.assertIn("Template missing at", content)
        self.assertNotIn("Error: environment template not found", content)
        self.assertIn("GITHUB_TOKEN=", content)
        self.assertIn("GH_TOKEN=", content)
        self.assertIn('runtime_dir="${resolved_repo_dir}/runtime"', content)
        self.assertIn('env_file="${runtime_dir}/copilot-telegram.env"', content)
        self.assertIn("__ENV_FILE__", content)
        self.assertIn("systemctl daemon-reload", content)

    def test_install_script_contains_no_python_fallback(self) -> None:
        content = INSTALL_SCRIPT.read_text(encoding="utf-8").lower()
        self.assertNotIn("python3", content)
        self.assertNotIn("python ", content)

    def test_env_template_contains_required_key(self) -> None:
        content = ENV_TEMPLATE.read_text(encoding="utf-8")
        self.assertIn("TELEGRAM_BOT_API_KEY=", content)
        self.assertIn("COPILOT_CLI_PATH=", content)
        self.assertIn("GITHUB_TOKEN=", content)
        self.assertIn("GH_TOKEN=", content)


if __name__ == "__main__":
    unittest.main()
