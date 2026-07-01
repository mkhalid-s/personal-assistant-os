from __future__ import annotations

import os
import sys
import tempfile
import unittest


class ModelSetupTest(unittest.TestCase):
    def test_recommendation_and_plan_defaults(self) -> None:
        from personal_assistant import model_setup

        rec = model_setup.recommended_model()
        self.assertEqual(rec["model"], "qwen2.5:0.5b")
        plan = model_setup.setup_plan(runtime="ollama", model="qwen2.5:0.5b")
        self.assertEqual(plan["runtime"], "ollama")
        self.assertEqual(plan["pull_command"], ["ollama", "pull", "qwen2.5:0.5b"])
        self.assertTrue(any(line == "MYOS_ROUTER_MODEL=qwen2.5:0.5b" for line in plan["env_lines"]))
        self.assertTrue(any(line.startswith(f"MYOS_ROUTER_COMMAND={sys.executable} ") for line in plan["env_lines"]))

    def test_dry_run_does_not_execute_download(self) -> None:
        from personal_assistant import model_setup

        plan = model_setup.setup_plan(runtime="ollama", model="qwen2.5:0.5b")
        called = {"value": False}
        original = model_setup.subprocess.run

        def fake_run(*args, **kwargs):
            called["value"] = True
            raise AssertionError("download should not run during dry-run")

        model_setup.subprocess.run = fake_run
        try:
            result = model_setup.apply_setup(plan, dry_run=True)
        finally:
            model_setup.subprocess.run = original
        self.assertEqual(result["status"], "dry_run")
        self.assertFalse(called["value"])

    def test_apply_constructs_safe_ollama_pull_and_wrapper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["MYOS_DB_PATH"] = os.path.join(tmp, "assistant.db")
            from personal_assistant import model_setup

            plan = model_setup.setup_plan(runtime="ollama", model="qwen2.5:0.5b")
            calls = []
            original = model_setup.subprocess.run

            class Result:
                returncode = 0
                stdout = "pulled"
                stderr = ""

            def fake_run(command, **kwargs):
                calls.append(command)
                return Result()

            model_setup.subprocess.run = fake_run
            try:
                result = model_setup.apply_setup(plan, dry_run=False)
            finally:
                model_setup.subprocess.run = original
                os.environ.pop("MYOS_DB_PATH", None)
            self.assertEqual(calls, [["ollama", "pull", "qwen2.5:0.5b"]])
            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["wrapper"].endswith("router_ollama.py"))

    def test_rejects_unknown_model(self) -> None:
        from personal_assistant import model_setup

        with self.assertRaises(ValueError):
            model_setup.setup_plan(runtime="ollama", model="unknown:1b")


if __name__ == "__main__":
    unittest.main()
