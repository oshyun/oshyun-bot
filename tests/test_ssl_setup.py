import os
import shlex
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from bot.core.paths import ROOT_DIR, setup_ssl_certificates


class TestSslSetup(unittest.TestCase):
    """macOS Python 환경에서 SSL 검증 실패 방지를 위한 CA 번들 자동 로드 검증."""

    def test_setup_ssl_certificates_preserves_existing_valid_file(self):
        with tempfile.NamedTemporaryFile("w+", delete=False) as f:
            valid_path = f.name

        try:
            with patch.dict(os.environ, {"SSL_CERT_FILE": valid_path}, clear=False):
                res = setup_ssl_certificates()
                self.assertEqual(res, valid_path)
                self.assertEqual(os.environ.get("SSL_CERT_FILE"), valid_path)
        finally:
            if os.path.exists(valid_path):
                os.remove(valid_path)

    def test_setup_ssl_certificates_injects_fallback_when_empty_or_invalid(self):
        # SSL_CERT_FILE, REQUESTS_CA_BUNDLE, CURL_CA_BUNDLE 이 무효하거나 비어있는 경우
        with patch.dict(
            os.environ,
            {
                "SSL_CERT_FILE": "/non/existent/cacert.pem",
                "REQUESTS_CA_BUNDLE": "",
                "CURL_CA_BUNDLE": "",
            },
            clear=False,
        ):
            res = setup_ssl_certificates()
            self.assertIsNotNone(res)
            self.assertTrue(os.path.isfile(res))
            self.assertEqual(os.environ.get("SSL_CERT_FILE"), res)
            self.assertEqual(os.environ.get("REQUESTS_CA_BUNDLE"), res)
            self.assertEqual(os.environ.get("CURL_CA_BUNDLE"), res)

    def test_common_sh_exports_ssl_cert_file_when_unset(self):
        """scripts/common.sh 실행 시 SSL_CERT_FILE 이 자동 export 되는지 검증."""
        common_sh = os.path.join(ROOT_DIR, "scripts", "common.sh")
        if not os.path.isfile(common_sh):
            self.skipTest("scripts/common.sh not found")

        cmd = [
            "bash",
            "-c",
            f'unset SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE && source "{common_sh}" && echo "CERT:$SSL_CERT_FILE"',
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0, f"common.sh failed: {res.stderr}")
        self.assertIn("CERT:", res.stdout)
        cert_path = res.stdout.strip().split("CERT:", 1)[1]
        self.assertTrue(bool(cert_path), "SSL_CERT_FILE should be populated by common.sh")
        self.assertTrue(os.path.isfile(cert_path), f"Populated certificate file does not exist: {cert_path}")


class TestShellCommandSafety(unittest.TestCase):
    """서브프로세스 및 셸 실행 시 'command not found: start_url' 류 따옴표/이스케이프 오류 방지 검증."""

    def test_subprocess_list_args_avoids_shell_parsing_errors(self):
        """리스트 형태로 인자를 넘기면 URL 내 '&start_url=/' 특수문자가 셸 명령어로 분리되지 않고 안전하게 전달됨."""
        target_param = "https://example.com/api?action=init&start_url=/"

        # [Good] 권장: shell=False + 리스트 형태 호출 (OS exec 레벨 전달)
        cmd = ["python3", "-c", "import sys; print(sys.argv[1])", target_param]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), target_param)
        self.assertNotIn("command not found", res.stderr)

    def test_shell_command_escape_with_shlex_quote(self):
        """불가피하게 셸 커맨드라인 문자열을 구성할 때 shlex.quote 로 안전하게 래핑됨을 검증."""
        json_with_start_url = '{"name": "oshyun", "start_url": "/"}'
        quoted = shlex.quote(json_with_start_url)

        # 단일 셸 호출에서 shlex.quote 로 감싼 인자가 개행/따옴표 미스 없이 온전히 전달됨
        cmd = ["bash", "-c", f"echo {quoted}"]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.assertEqual(res.returncode, 0)
        self.assertEqual(res.stdout.strip(), json_with_start_url)
        self.assertNotIn("command not found", res.stderr)

    def test_unquoted_shell_invocation_causes_start_url_command_error(self):
        """[재현 및 분석] 만약 따옴표 없이 셸에 개행/JSON이 전달되면 'command not found: start_url' 이 발생함을 검증."""
        # 셸에서 인용부호 없이 멀티라인 JSON이 전달되면 두 번째 줄의 "start_url": "/" 에서
        # 셸 파서가 "start_url": 을 명령어로 파싱하려 하여 command not found 발생
        broken_shell_cmd = """
bash -c '
echo {"name": "oshyun"
"start_url": "/"
}
' 2>&1 || true
"""
        res = subprocess.run(broken_shell_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        combined_output = (res.stdout + res.stderr).lower()
        self.assertTrue(
            "start_url" in combined_output and ("not found" in combined_output or "syntax error" in combined_output),
            f"Expected command not found for start_url, got: {combined_output}",
        )


if __name__ == "__main__":
    unittest.main()
