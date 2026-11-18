"""Discovery boundaries use generated metadata/config only, never user files."""
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from agent_hub.discovery import _codex_model, _dsh_default, discover


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.apps = self.root / "Applications"
        self.home.mkdir()
        self.apps.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def config(self, relative, text):
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def app(self, name, metadata):
        directory = self.apps / (name + ".app") / "Contents"
        directory.mkdir(parents=True)
        with (directory / "Info.plist").open("wb") as stream:
            plistlib.dump(metadata, stream)
        return directory / "Info.plist"

    def test_only_top_level_codex_model_is_selected(self):
        self.config(".codex/config.toml", 'model = "configured-model"\n'
                    'api_key = "FAKE_SECRET_NEVER_EXPORT"\n'
                    '[profiles.special]\nmodel = "profile-model"\n')
        result = _codex_model(self.home / ".codex")
        self.assertEqual(result["value"], "configured-model")
        self.assertEqual(result["confidence"], "configured_default")
        self.assertNotIn("FAKE_SECRET", json.dumps(result))
        self.assertNotIn("profile-model", json.dumps(result))
        self.config(".codex/config.toml", '[profiles.special]\nmodel = "profile-only"\n')
        self.assertIsNone(_codex_model(self.home / ".codex")["value"])

    def test_dsh_rejects_ambiguous_or_executable_model_config(self):
        path = ".dsh/profiles/desktop/cordis.patch.yml"
        self.config(path, "- id: agent-default-model\n  config:\n    model: one\n"
                    "- id: agent-default-model\n  config:\n    model: two\n")
        self.assertIsNone(_dsh_default(self.home / ".dsh", "desktop")["value"])
        self.config(path, "- id: agent-default-model\n  config:\n    model: !!js getModel()\n")
        self.assertIsNone(_dsh_default(self.home / ".dsh", "desktop")["value"])
        self.config(path, "- id: unrelated\n  config:\n    rows:\n"
                    "      - name: '@deepseek-ai/dsh-agent-default-model'\n"
                    "        config:\n          model: nested-unrelated\n")
        self.assertIsNone(_dsh_default(self.home / ".dsh", "desktop")["value"])

    def test_dsh_exports_only_documented_default_fields(self):
        self.config(".dsh/profiles/desktop/cordis.patch.yml",
                    "- name: '@deepseek-ai/dsh-agent-default-model'\n  config:\n"
                    "    model: 'configured-qwen'\n    provider: safe-provider\n"
                    "    apiKey: FAKE_PROVIDER_SECRET\n"
                    "- id: other\n  config:\n    model: unrelated-model\n")
        result = _dsh_default(self.home / ".dsh", "desktop")
        self.assertEqual(result["value"], "configured-qwen")
        self.assertEqual(result["provider"], "safe-provider")
        self.assertEqual(result["profile"], "desktop")
        self.assertNotIn("FAKE_PROVIDER_SECRET", json.dumps(result))
        self.assertNotIn("unrelated-model", json.dumps(result))

    def test_discovery_never_opens_credential_files_and_unknown_apps_stay_unverified(self):
        allowed_paths = {
            self.config(".codex/config.toml", 'model = "safe-model"\napi_key = "FAKE_SECRET_1"\n'),
            self.config(".dsh/profiles/desktop/cordis.patch.yml",
                        "- id: agent-default-model\n  config:\n    model: safe-qwen\n    provider: safe-provider\n"),
        }
        self.config(".codex/auth.json", '{"token":"MUST_NOT_READ_AUTH"}')
        self.config(".dsh/.credentials.yaml", "key: MUST_NOT_READ_CREDENTIALS")
        allowed_paths.add(self.app("WorkBuddy", {"CFBundleIdentifier": "com.tencent.workbuddy.mac",
                          "CFBundleName": "WorkBuddy", "CFBundleShortVersionString": "test-version",
                          "CFBundleURLTypes": {"unsupported_shape": "FAKE_METADATA_SECRET"}}))
        allowed_paths.add(self.app("Unknown", ["unsupported", "root", "shape"]))
        broken = self.apps / "Broken.app" / "Contents" / "Info.plist"
        broken.parent.mkdir(parents=True)
        broken.write_text("<plist>invalid", encoding="utf-8")
        allowed_paths.add(broken)
        allowed_paths.add(self.home / ".dsh/profiles/headless/cordis.patch.yml")
        original_open = Path.open
        reads = []

        def guarded_open(path, *args, **kwargs):
            self.assertIn(path, allowed_paths, "Discovery opened an unexpected file: " + str(path))
            reads.append(path)
            return original_open(path, *args, **kwargs)

        with patch("agent_hub.discovery.shutil.which", return_value=None), \
             patch("agent_hub.discovery.platform.system", return_value="TestOS"), \
             patch("agent_hub.discovery.platform.mac_ver", return_value=("", ("", "", ""), "")), \
             patch("agent_hub.discovery.subprocess.run", side_effect=AssertionError("No subprocess expected")), \
             patch.object(Path, "open", guarded_open):
            result = discover(probe_cli=False, app_roots=[self.apps], user_home=self.home)
        agents = {agent["id"]: agent for agent in result["agents"]}
        self.assertTrue(agents["workbuddy"]["installed"])
        self.assertEqual(agents["workbuddy"]["automation"]["status"], "not_verified")
        self.assertEqual(agents["workbuddy"]["capabilities"], [])
        self.assertEqual(agents["workbuddy"]["tests"], [])
        self.assertEqual(agents["codex"]["automation"]["status"], "not_verified")
        self.assertFalse(agents["codex"]["installed"])
        self.assertEqual(agents["codex"]["model"]["value"], "safe-model")
        self.assertEqual(agents["deepseek"]["model"]["value"], "safe-qwen")
        self.assertEqual(agents["doubao"]["transport"], "file_bridge")
        self.assertIsNone(agents["doubao"]["model"]["value"])
        serialized = json.dumps(result)
        for secret in ("FAKE_SECRET_1", "MUST_NOT_READ_AUTH", "MUST_NOT_READ_CREDENTIALS", "FAKE_METADATA_SECRET"):
            self.assertNotIn(secret, serialized)
        self.assertFalse(any(path.name in ("auth.json", ".credentials.yaml") for path in reads))


if __name__ == "__main__":
    unittest.main()
