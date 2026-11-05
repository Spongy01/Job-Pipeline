import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "scripts"))
import llm  # noqa: E402


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def chat(self, **kwargs):
        if kwargs.get("keep_alive") == 0:
            raise RuntimeError("cannot unload")
        self.calls.append(kwargs)
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(r, Exception):
            raise r
        return {"message": {"content": r}}


def parse_obj(raw):
    value = json.loads(llm.extract_json(raw))
    if "ok" not in value:
        raise ValueError("missing ok")
    return value


MESSAGES = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]


class GenerateJsonTests(unittest.TestCase):
    def test_success_first_try(self):
        client = FakeClient(['```json\n{"ok": 1}\n```'])
        value, error, attempts = llm.generate_json(client, "m", MESSAGES, parse_obj, 4096, False, 1)
        self.assertEqual((value, error, attempts), ({"ok": 1}, None, 1))
        self.assertNotIn("format", client.calls[0])
        self.assertEqual(client.calls[0]["options"], {"temperature": 0, "num_ctx": 4096})

    def test_generate_json_repairs_once(self):
        client = FakeClient(["garbage", '{"ok": 1}'])
        value, error, attempts = llm.generate_json(client, "m", MESSAGES, parse_obj, 4096, False, 1)
        self.assertEqual((value, error, attempts), ({"ok": 1}, None, 2))
        last = client.calls[1]["messages"][-1]["content"]
        self.assertIn("not valid", last)
        self.assertEqual(client.calls[1]["messages"][2]["content"], "garbage")

    def test_generate_json_gives_up(self):
        client = FakeClient(['{"nope": 1}'])
        value, error, attempts = llm.generate_json(client, "m", MESSAGES, parse_obj, 4096, False, 1)
        self.assertIsNone(value)
        self.assertEqual(attempts, 2)
        self.assertIn("missing ok", error)

    def test_transport_error_is_reported_not_raised(self):
        client = FakeClient([RuntimeError("boom")])
        value, error, attempts = llm.generate_json(client, "m", MESSAGES, parse_obj, 4096, False, 0)
        self.assertEqual((value, attempts), (None, 1))
        self.assertIn("boom", error)

    def test_think_rejected_is_retried_without_it(self):
        client = FakeClient([RuntimeError("model does not support think"), '{"ok": 1}'])
        value, _, _ = llm.generate_json(client, "m", MESSAGES, parse_obj, 4096, False, 0)
        self.assertEqual(value, {"ok": 1})
        self.assertIn("think", client.calls[0])
        self.assertNotIn("think", client.calls[1])


class OtherHelperTests(unittest.TestCase):
    def test_generate_text_returns_raw_reply(self):
        self.assertEqual(llm.generate_text(FakeClient(["# Spec\ntext"]), "m", MESSAGES, 4096, False), "# Spec\ntext")

    def test_unload_ignores_errors(self):
        llm.unload_model(FakeClient(["x"]), "m")  # the fake raises on keep_alive=0

    def test_load_prompt_area_and_version_checks(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "prompts" / "boost").mkdir(parents=True)
        (tmp / "prompts" / "boost" / "v3.py").write_text("VERSION = 'v3'\n")
        self.assertEqual(llm.load_prompt("v3", tmp, "boost").VERSION, "v3")
        for bad in ("v4", "../x", "v3.py", ""):
            with self.assertRaises(llm.LLMError):
                llm.load_prompt(bad, tmp, "boost")
        with self.assertRaises(llm.LLMError):
            llm.load_prompt("v3", tmp, "enrich")

    def test_check_models(self):
        class Lister:
            def list(self):
                return {"models": [{"model": "a:latest"}, {"model": "b:1"}]}
        llm.check_models(Lister(), ["a", "b:1"])
        with self.assertRaises(llm.LLMError):
            llm.check_models(Lister(), ["c"])

        class Down:
            def list(self):
                raise ConnectionError("refused")
        with self.assertRaises(llm.LLMError) as cm:
            llm.check_models(Down(), ["a"])
        self.assertIn("ollama serve", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
