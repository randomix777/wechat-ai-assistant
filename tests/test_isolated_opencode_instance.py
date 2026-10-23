"""私有 OpenCode 实例的隔离与提示词护栏。

这里守的是三个曾经真的坏掉过的东西：

1. ``order-parse`` 的**系统提示词**：后端发的是 ``PromptPackage(system="")``，
   提示词只可能来自 agent 的 markdown 文件。文件不在，模型就收到零指令 ——
   而实例**照样**返回 200，只有解析结果不对。
2. **模型能不能解析出来**：models.dev 里 opencode-go 的目录是旧的，既没有
   qwen3.6-plus 也没有 baseURL，继承目录必然报 "Model not found"。
3. **隔离是不是真的**：provider 列表会被环境变量污染（models.dev 按环境变量名
   认 provider），所以判据不能是"provider 列表看起来对"，而必须是
   "实例实际加载的配置全都在私有目录里"。
"""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "examples" / "prepare_isolated_opencode.py"
SPEC = importlib.util.spec_from_file_location("prepare_isolated_opencode_test_module", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

SYSTEM_PROMPT = MODULE.SYSTEM_PROMPT


class PrivateConfigTests(unittest.TestCase):
    def test_config_declares_the_provider_instead_of_inheriting_it(self):
        config = MODULE.build_config()
        provider = config["provider"][MODULE.PROVIDER_ID]
        self.assertEqual(provider["options"]["baseURL"], "https://opencode.ai/zen/go/v1")
        self.assertEqual(provider["options"]["apiKey"], "{env:OPENCODE_GO_API_KEY}")
        self.assertIn(MODULE.MODEL_ID, provider["models"])

    def test_config_has_no_forbidden_keys(self):
        config = MODULE.build_config()
        for key in MODULE.FORBIDDEN_CONFIG_KEYS:
            self.assertNotIn(key, config, f"私有配置里不该有 {key}")

    def test_config_declares_only_the_order_parse_agent(self):
        agents = MODULE.build_config()["agent"]
        self.assertIn("order-parse", agents)
        self.assertEqual(agents["order-parse"]["mode"], "primary")
        self.assertEqual(agents["order-parse"]["permission"]["*"], "deny")

    def test_provider_id_and_model_id_are_not_guessed(self):
        self.assertEqual(MODULE.PROVIDER_ID, "opencode-go")
        self.assertEqual(MODULE.MODEL_ID, "qwen3.6-plus")


class AgentPromptTests(unittest.TestCase):
    def test_prompt_file_is_the_single_source_of_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = MODULE.write_agent_prompt(Path(tmp))
            self.assertEqual(target, Path(tmp) / "agents" / "order-parse.md")
            self.assertEqual(target.read_text(encoding="utf-8"), SYSTEM_PROMPT)

    def test_prompt_file_has_no_carriage_returns(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = MODULE.write_agent_prompt(Path(tmp))
            self.assertNotIn("\r", target.read_text(encoding="utf-8"))

    def test_prompt_is_not_empty(self):
        self.assertTrue(SYSTEM_PROMPT.strip())
        self.assertIn("待分析的数据", SYSTEM_PROMPT)


class CredentialEnvironmentTests(unittest.TestCase):
    def test_provider_credentials_are_recognised(self):
        for name in ("OPENROUTER_API_KEY", "AGNES_API_KEY", "OPENCODE_API_KEY", "CLOUDFLARE_API_TOKEN"):
            self.assertTrue(MODULE._is_credential_env(name), name)

    def test_the_go_key_survives(self):
        self.assertFalse(MODULE._is_credential_env("OPENCODE_GO_API_KEY"))

    def test_unrelated_variables_survive(self):
        for name in ("PATH", "SystemRoot", "WECHAT_AI_OPENCODE_PROVIDER"):
            self.assertFalse(MODULE._is_credential_env(name), name)

    def test_private_env_does_not_carry_provider_credentials(self):
        env = MODULE.private_env(Path(tempfile.gettempdir()) / "unused.pw", 0)
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("AGNES_API_KEY", env)
        self.assertNotIn("OPENCODE_API_KEY", env)


class LoadedConfigPathTests(unittest.TestCase):
    def _log(self, directory: Path, lines: list[str]) -> Path:
        path = directory / "opencode.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_loading_paths_are_read_from_the_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = self._log(Path(tmp), [
                'timestamp=1 level=INFO message=loading path="C:\\a\\opencode.json"',
                'timestamp=2 level=INFO message=other thing',
            ])
            self.assertEqual(MODULE.loaded_config_paths(log), ["C:\\a\\opencode.json"])

    def test_a_missing_log_is_not_an_empty_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(MODULE.loaded_config_paths(Path(tmp) / "nope.log", wait=0.1), [])


class VerifyIsolationTests(unittest.TestCase):
    """``verify_isolation`` 的每一条判据都要有反例护着。"""

    def setUp(self):
        self._real_http = MODULE.http
        self._real_log = MODULE.INSTANCE_LOG
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.private_root = Path(self._tmp.name) / "private"
        self.private_root.mkdir()
        MODULE.PRIVATE_ROOT = self.private_root
        self.log = self.private_root / "opencode.log"
        self.log.write_text(
            'timestamp=1 level=INFO message=loading path="%s"\n'
            % str(self.private_root / "config" / "opencode" / "opencode.json"),
            encoding="utf-8",
        )
        MODULE.INSTANCE_LOG = self.log
        self.responses = {
            "/global/health": {"healthy": True, "version": "1.18.18"},
            "/agent": [{"name": "order-parse", "prompt": SYSTEM_PROMPT}],
            "/provider": {
                "connected": sorted(MODULE.BUILTIN_CONNECTED | {MODULE.PROVIDER_ID}),
                "all": [{"id": MODULE.PROVIDER_ID, "models": {MODULE.MODEL_ID: {"name": "x"}}}],
            },
        }
        MODULE.http = lambda url, password, timeout=8.0, user="wechat_ai_assistant": self._route(url)
        self.addCleanup(self._restore)

    def _restore(self):
        MODULE.http = self._real_http
        MODULE.INSTANCE_LOG = self._real_log

    def _route(self, url: str):
        for path, payload in self.responses.items():
            if url.endswith(path):
                return payload
        raise AssertionError(f"unexpected url {url}")

    def test_a_healthy_private_instance_passes(self):
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertTrue(ok, report.get("error"))

    def test_an_agent_without_a_prompt_fails(self):
        self.responses["/agent"] = [{"name": "order-parse"}]
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn("提示词", report["error"])

    def test_an_agent_with_a_different_prompt_fails(self):
        self.responses["/agent"] = [{"name": "order-parse", "prompt": "别的提示词"}]
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn("SYSTEM_PROMPT", report["error"])

    def test_an_inherited_agent_fails(self):
        self.responses["/agent"] = [
            {"name": "order-parse", "prompt": SYSTEM_PROMPT},
            {"name": "build"},
        ]
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn("build", report["error"])

    def test_a_leaked_provider_fails(self):
        self.responses["/provider"]["connected"] = sorted(
            MODULE.BUILTIN_CONNECTED | {MODULE.PROVIDER_ID, "openrouter"}
        )
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn("openrouter", report["error"])

    def test_a_missing_model_fails(self):
        self.responses["/provider"]["all"] = [{"id": MODULE.PROVIDER_ID, "models": {}}]
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn(MODULE.MODEL_ID, report["error"])

    def test_a_global_config_in_the_log_fails(self):
        self.log.write_text(
            'timestamp=1 level=INFO message=loading path="C:\\Users\\x\\.config\\opencode\\opencode.json"\n',
            encoding="utf-8",
        )
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn("全局配置", report["error"])

    def test_an_unreadable_log_fails_instead_of_passing_silently(self):
        MODULE.INSTANCE_LOG = self.private_root / "absent.log"
        ok, report = MODULE.verify_isolation("http://127.0.0.1:1", "pw")
        self.assertFalse(ok)
        self.assertIn("加载日志", report["error"])


class PromptSourceOfTruthTests(unittest.TestCase):
    def test_the_prompt_comes_from_order_agent(self):
        self.assertEqual(MODULE.PROMPT_VERSION, "order-parse-v1")
        self.assertIn("待分析的数据", MODULE.SYSTEM_PROMPT)

    def test_the_prompt_names_the_fields_the_contract_validates(self):
        from order_agent.contract import OUTPUT_KEYS
        for key in OUTPUT_KEYS:
            self.assertIn(f'"{key}"', MODULE.SYSTEM_PROMPT, f"提示词里没有 {key}")


if __name__ == "__main__":
    unittest.main()
