"""Tests for the public-safe publish routine (build -> clean -> verify -> commit -> push).

git is never called for real: subprocess.run is patched. The public folder is
never touched: every test uses a temporary directory.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

import build_public_repo  # noqa: E402
import publish_public  # noqa: E402
import verify_public_safe  # noqa: E402


def scratch_dir():
    base = os.environ.get("LOCALAPPDATA")
    root = Path(base) / "Temp" if base else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return tempfile.mkdtemp(prefix="publish_public_", dir=root)


class ExclusionTests(unittest.TestCase):
    def test_private_files_are_never_selected(self):
        tracked = [
            "reach/about_me.json",
            "reach/about_me.example.json",
            "career_pipeline_v2.sqlite3",
            "backups/career_pipeline_v2.20260904T144654284762Z.bak.sqlite3",
            "career_pipeline_v2.backup-20260903-153627.sqlite3",
            "career_pipeline_v2_export.json",
            ".env",
            ".env.local",
            "reach/drafts.py",
            "pipeline_v2.py",
            "reference_cv_2027/data/career_master.yaml",
        ]
        kept = build_public_repo.select_files(tracked)
        self.assertEqual(kept, ["reach/about_me.example.json", "reach/drafts.py", "pipeline_v2.py"])

    def test_build_into_temp_dir_skips_excluded_and_depersonalizes(self):
        src = Path(scratch_dir())
        dst = Path(scratch_dir()) / "public"
        (src / "reach").mkdir()
        (src / "reach" / "about_me.json").write_text('{"phone": "+000 000000000"}', encoding="utf-8")
        (src / "reach" / "drafts.py").write_text("AUTHOR = 'the candidate'\n", encoding="utf-8")
        (src / "career_pipeline_v2.sqlite3").write_bytes(b"\x00")
        (src / ".env").write_text("KEY=1", encoding="utf-8")
        tracked = ["reach/about_me.json", "reach/drafts.py", "career_pipeline_v2.sqlite3", ".env"]
        stats = build_public_repo.build(src=src, dst=dst, tracked=tracked)
        self.assertEqual(stats["copied"], 1)
        self.assertFalse((dst / "reach" / "about_me.json").exists())
        self.assertFalse((dst / "career_pipeline_v2.sqlite3").exists())
        self.assertFalse((dst / ".env").exists())
        self.assertIn("the candidate", (dst / "reach" / "drafts.py").read_text(encoding="utf-8"))

    def test_build_preserves_public_git_dir_and_public_only_files_but_drops_stale_copies(self):
        src = Path(scratch_dir())
        dst = Path(scratch_dir()) / "public"
        (src / "keep.py").write_text("x = 1\n", encoding="utf-8")
        (dst / ".git").mkdir(parents=True)
        (dst / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (dst / "README.md").write_text("public readme\n", encoding="utf-8")
        (dst / "stale.py").write_text("old\n", encoding="utf-8")
        build_public_repo.build(src=src, dst=dst, tracked=["keep.py"])
        self.assertTrue((dst / ".git" / "HEAD").exists())
        self.assertTrue((dst / "README.md").exists())
        self.assertTrue((dst / "keep.py").exists())
        self.assertFalse((dst / "stale.py").exists())

    def test_verify_returns_failures_for_phone_and_clean_for_neutral_tree(self):
        root = Path(scratch_dir())
        (root / "a.py").write_text("x = 1\n", encoding="utf-8")
        self.assertEqual(verify_public_safe.verify(root), [])
        (root / "b.md").write_text("call +000 000000000\n", encoding="utf-8")
        self.assertTrue(any("real phone" in f for f in verify_public_safe.verify(root)))


class PublishTests(unittest.TestCase):
    def setUp(self):
        self.public = Path(scratch_dir())
        self.calls = []
        self.private_msg = "Clean the search snippets and keep the person's real headline\n"
        self.public_msg = "Older public commit\n"
        self.dirty = " M file.py\n"
        patches = [
            mock.patch.object(publish_public, "build_public", lambda src, dst: {"copied": 1}),
            mock.patch.object(publish_public, "clean_public", lambda dst: []),
            mock.patch.object(publish_public, "verify_public", lambda dst: []),
            mock.patch.object(publish_public.subprocess, "run", self.fake_run),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def fake_run(self, args, **kwargs):
        self.calls.append((tuple(args), kwargs.get("cwd")))
        out = ""
        if args[:2] == ["git", "-C"] and "log" in args:
            out = self.private_msg
        elif "log" in args:
            out = self.public_msg
        elif "status" in args:
            out = self.dirty
        elif "rev-parse" in args:
            out = "abc1234def\n"
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")

    def git_verbs(self):
        return [c[0][1] if c[0][1] != "-C" else c[0][3] for c in self.calls if c[0][0] == "git"]

    def run_publish(self, *flags):
        out = []
        code = publish_public.main(list(flags), public_dir=self.public, out=out.append)
        return code, "\n".join(out)

    def test_aborts_with_exit_2_when_verify_is_not_clean(self):
        with mock.patch.object(publish_public, "verify_public", lambda dst: ["real phone  x.md:1"]):
            code, text = self.run_publish()
        self.assertEqual(code, 2)
        self.assertIn("real phone", text)
        self.assertNotIn("commit", self.git_verbs())
        self.assertNotIn("push", self.git_verbs())

    def test_commits_with_private_head_message_and_pushes(self):
        code, text = self.run_publish()
        self.assertEqual(code, 0)
        commit = [c for c in self.calls if c[0][0] == "git" and c[0][1] == "commit"][0]
        self.assertEqual(commit[1], str(self.public))
        self.assertEqual(commit[0], ("git", "commit", "-F", "-"))
        self.assertIn(("git", "add", "-A"), [c[0] for c in self.calls])
        self.assertIn("push", self.git_verbs())
        self.assertIn("published abc1234def -> https://github.com/your-github-handle/career-pipeline/commit/abc1234def", text)

    def test_commit_message_is_the_private_head_message(self):
        with mock.patch.object(publish_public.subprocess, "run", side_effect=self.fake_run) as run:
            self.run_publish()
        commit_call = [c for c in run.call_args_list if c.args[0][:2] == ["git", "commit"]][0]
        self.assertEqual(commit_call.kwargs["input"], self.private_msg)

    def test_no_push_skips_push(self):
        code, text = self.run_publish("--no-push")
        self.assertEqual(code, 0)
        self.assertNotIn("push", self.git_verbs())
        self.assertIn("published abc1234def", text)

    def test_dry_run_verifies_but_never_commits(self):
        code, text = self.run_publish("--dry-run", "--no-push")
        self.assertEqual(code, 0)
        self.assertNotIn("add", self.git_verbs())
        self.assertNotIn("commit", self.git_verbs())
        self.assertNotIn("push", self.git_verbs())
        self.assertIn("dry-run", text)
        self.assertIn(self.private_msg.strip(), text)

    def test_nothing_to_commit_exits_zero_without_commit(self):
        self.dirty = ""
        code, text = self.run_publish()
        self.assertEqual(code, 0)
        self.assertNotIn("commit", self.git_verbs())
        self.assertIn("nothing to publish", text)

    def test_if_behind_exits_silently_when_public_head_matches(self):
        self.public_msg = self.private_msg
        code, text = self.run_publish("--if-behind")
        self.assertEqual(code, 0)
        self.assertEqual(text, "")
        self.assertEqual([v for v in self.git_verbs() if v not in ("log",)], [])

    def test_if_behind_publishes_when_messages_differ(self):
        code, text = self.run_publish("--if-behind")
        self.assertEqual(code, 0)
        self.assertIn("push", self.git_verbs())

    def test_public_dir_comes_from_env_when_not_given(self):
        with mock.patch.dict(os.environ, {"PUBLIC_REPO_DIR": str(self.public)}):
            self.assertEqual(publish_public.public_dir_from_env(), self.public)


class HookTests(unittest.TestCase):
    def test_tracked_hook_is_posix_sh_backgrounds_publish_and_never_fails_the_commit(self):
        hook = (PROJECT_ROOT / "scripts" / "hooks" / "post-commit").read_text(encoding="utf-8")
        self.assertTrue(hook.startswith("#!/bin/sh\n"))
        self.assertNotIn("\r", hook)
        self.assertIn("publish_public.py", hook)
        self.assertIn("&", hook.split("publish_public.py", 1)[1])
        self.assertIn("exit 0", hook)
        self.assertIn("publish_public.log", hook)


if __name__ == "__main__":
    unittest.main()
