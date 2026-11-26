import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from factor_rotation import passes, transcripts

HOME, REPO = Path("/Users/owner"), Path("/Users/owner/Projects/Book")


def line(kind: str, **more) -> str:
    return json.dumps({"type": kind, "timestamp": "2026-11-25T01:30:00Z", **more})


def session() -> list[str]:
    said = lambda *blocks: {"content": list(blocks)}  # noqa: E731
    return [
        line("attachment", attachment={"type": "session_context", "context": {"userEmail": "The user's email address is owner@example.com."}}),
        line("attachment", attachment={"type": "instructions", "files": [{"path": "/Users/owner/.claude/memory/MEMORY.md", "content": "private notes"}]}),
        line("user", version="2.1.331", entrypoint="sdk-cli", message={"content": 'Run the NIGHT PASS. cd "/Users/owner/Projects/Book"'}),
        line("assistant", requestedModel="claude-fable-5-2", effort="high", thinkingDurationMs=400,
             message=said({"type": "thinking", "thinking": "", "signature": "x" * 900})),
        line("assistant", requestedModel="claude-fable-5-2", message=said({"type": "text", "text": "Screen is built."},
             {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "uv run fr paper desk"}})),
        line("user", toolUseResult={"stdout": "dup"}, message=said({"type": "tool_result", "tool_use_id": "t1", "content": [
            {"type": "text", "text": "paper account DU1234567 on port 4002\nsent to bot 123456789:" + "A" * 35 +
             "\n<system-reminder>\nmemory: owner@example.com\n</system-reminder>\ntopic s3cret-topic-value"}]})),
        line("attachment", attachment={"type": "queued_command", "timestamp": "2026-11-25T01:40:00Z",
                                       "prompt": "<task-notification><output-file>/private/tmp/-Users-owner-Projects-Book/x.output</output-file><result>GDX write-up</result>"}),
        line("api-request-blob", message=said({"type": "text", "text": "owner@example.com"})),
        line("cost-state", totalCostUSD=13.8024, modelUsage={"claude-fable-5-2": {"outputTokens": 10, "costUSD": 8.2}}),
        "not json",
    ]


class Slim(unittest.TestCase):
    def setUp(self):
        self.scrub = transcripts.scrubber(private=["s3cret-topic-value"], home=HOME, repo=REPO)
        self.facts, self.records = transcripts.slim(session(), self.scrub)

    def test_keeps_the_prompt_the_judge_its_calls_results_and_notices_in_order(self):
        self.assertEqual([r["kind"] for r in self.records], ["prompt", "thinking", "judge", "call", "result", "notice"])
        self.assertEqual(self.records[3]["input"], {"command": "uv run fr paper desk"})
        self.assertEqual(self.records[4]["tool"], "Bash")
        self.assertIn("GDX write-up", self.records[5]["text"])

    def test_nothing_private_survives(self):
        text = json.dumps([self.facts, self.records])
        for private in ("owner@example.com", "DU1234567", "s3cret-topic-value", "/Users/owner", "-Users-owner-", "A" * 35,
                        "system-reminder", "private notes", "MEMORY.md", "signature", "x" * 50, "dup"):
            self.assertNotIn(private, text)
        self.assertEqual(self.records[0]["text"], 'Run the NIGHT PASS. cd "."')
        self.assertIn("paper account <account> on port 4002", self.records[4]["text"])

    def test_the_facts_say_what_ran(self):
        self.assertEqual(self.facts["model"], "claude-fable-5-2")
        self.assertEqual((self.facts["claude_code"], self.facts["entrypoint"], self.facts["cost_usd"]), ("2.1.331", "sdk-cli", 13.8))
        self.assertEqual(self.facts["usage"], {"claude-fable-5-2": {"outputTokens": 10}})
        self.assertFalse(self.facts["thinking_saved"])
        self.assertEqual(self.facts["counts"]["call"], 1)

    def test_thinking_text_is_kept_when_claude_code_saved_it(self):
        lines = [line("assistant", requestedModel="claude-opus-5-5", message={"content": [{"type": "thinking", "thinking": "GDX is stretched."}]})]
        facts, records = transcripts.slim(lines, self.scrub)
        self.assertTrue(facts["thinking_saved"])
        self.assertEqual(records[0]["text"], "GDX is stretched.")


class Archive(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.root = Path(self.dir.name)
        self.projects = self.root / "projects"
        (self.projects / "-Users-owner-Projects-Book").mkdir(parents=True)
        (self.projects / "-Users-owner-Projects-Book" / "abc.jsonl").write_text("\n".join(session()) + "\n")
        for patcher in (mock.patch.object(transcripts, "PROJECTS", self.projects), mock.patch.object(transcripts, "secrets", return_value=[]),
                        mock.patch.object(transcripts, "code_version", return_value="cf355e0")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_a_pass_leaves_its_transcript_in_the_journal(self):
        kept = passes._keep("abc", self.root / "journal", "2026-11-25-night")
        self.assertEqual(kept, str(self.root / "journal/research/transcripts/2026-11-25-night.jsonl.gz"))
        about, *records = transcripts.read(Path(kept))
        self.assertEqual((about["kind"], about["session"], about["pass"], about["code"]), ("about", "2026-11-25", "night", "cf355e0"))
        self.assertEqual(len(records), 6)

    def test_the_same_session_gives_the_same_bytes(self):
        first = Path(passes._keep("abc", self.root / "a", "2026-11-25-night")).read_bytes()
        self.assertEqual(first, Path(passes._keep("abc", self.root / "b", "2026-11-25-night")).read_bytes())

    def test_a_missing_or_broken_transcript_never_fails_the_pass(self):
        self.assertIsNone(passes._keep(None, self.root / "journal", "2026-11-25-night"))
        self.assertIsNone(passes._keep("nope", self.root / "journal", "2026-11-25-night"))
        with mock.patch.object(transcripts, "archive", side_effect=OSError("disk full")):
            self.assertIsNone(passes._keep("abc", self.root / "journal", "2026-11-25-night"))

    def test_grep_cannot_read_an_old_transcript(self):
        kept = Path(passes._keep("abc", self.root / "journal", "2026-11-25-night"))
        self.assertNotIn(b"Screen is built", kept.read_bytes())


class Secrets(unittest.TestCase):
    def test_private_settings_come_from_the_environment_and_the_env_files(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict("os.environ", {"FR_NTFY_TOPIC": "topic-in-env", "FR_PAPER_PORT": "4002000"}, clear=True):
            Path(d, ".env.paper").write_text("# login\nTWS_USERID=paperuser\nTWS_PASSWORD=\"hunter2hunter2\"\nTRADING_MODE=paper-mode\n")
            with mock.patch("pathlib.Path.cwd", return_value=Path(d)), mock.patch.object(transcripts, "Path", lambda name: Path(d, name)):
                found = transcripts.secrets()
        self.assertEqual(set(found), {"topic-in-env", "paperuser", "hunter2hunter2"})
        self.assertEqual(found[0], "hunter2hunter2")   # longest first


if __name__ == "__main__":
    unittest.main()
