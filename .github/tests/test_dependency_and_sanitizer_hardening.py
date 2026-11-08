import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
BUILD = WORKFLOWS / "build-and-test.yml"
DEPLOY = WORKFLOWS / "deploy-testflight.yml"
PATTERNS = ROOT / ".github" / "sanitize-patterns.json"

# Synthetic stand-ins for private dependency details; none of these may reach step output.
PRIVATE_GEM = "synthetic-private-gem"
PRIVATE_HOST = "example.invalid"
GUARD_ERROR = "::error::Gemfile.lock must resolve only from https://rubygems.org/"

CLEAN_GEM_SECTION = """GEM
  remote: https://rubygems.org/
  specs:
    rake (13.0.0)
"""

LOCKFILE_TAIL = """
PLATFORMS
  arm64-darwin

DEPENDENCIES
  rake

BUNDLED WITH
   2.4.0
"""

CLEAN_LOCKFILE = CLEAN_GEM_SECTION + LOCKFILE_TAIL

DISALLOWED_LOCKFILES = {
    "git source": (
        "GIT\n"
        "  remote: https://git.example.invalid/synthetic-private-gem.git\n"
        "  revision: 0123456789abcdef0123456789abcdef01234567\n"
        "  specs:\n"
        "    synthetic-private-gem (1.0.0)\n\n" + CLEAN_LOCKFILE,
        "remote: https://git.example.invalid/synthetic-private-gem.git",
    ),
    "path source": (
        "PATH\n"
        "  remote: ../synthetic-private-gem\n"
        "  specs:\n"
        "    synthetic-private-gem (1.0.0)\n\n" + CLEAN_LOCKFILE,
        "remote: ../synthetic-private-gem",
    ),
    "foreign gem server": (
        CLEAN_LOCKFILE.replace("https://rubygems.org/", "https://gems.example.invalid/"),
        "remote: https://gems.example.invalid/",
    ),
    "additional gem server": (
        CLEAN_LOCKFILE.replace(
            "  remote: https://rubygems.org/\n",
            "  remote: https://rubygems.org/\n  remote: https://gems.example.invalid/\n",
        ),
        "remote: https://gems.example.invalid/",
    ),
    "look-alike host": (
        CLEAN_LOCKFILE.replace("https://rubygems.org/", "https://rubygems.org.example.invalid/"),
        "remote: https://rubygems.org.example.invalid/",
    ),
    "plain http": (
        CLEAN_LOCKFILE.replace("https://rubygems.org/", "http://rubygems.org/"),
        "remote: http://rubygems.org/",
    ),
    "plugin source": (
        "PLUGIN SOURCE\n"
        "  remote: https://plugins.example.invalid/\n"
        "  type: synthetic\n"
        "  specs:\n"
        "    synthetic-private-gem (1.0.0)\n\n" + CLEAN_LOCKFILE,
        "remote: https://plugins.example.invalid/",
    ),
}

# Prints private-looking output on every call so tests can prove it is never echoed.
STUB_BUNDLE = """#!/usr/bin/env bash
set -u
state=../state
printf '%s\\n' "$*" >> "$state/calls.log"
echo "Fetching synthetic-private-gem 1.0.0 from https://gems.example.invalid/"
echo "Installing synthetic-private-gem 1.0.0" >&2
case "${1:-}" in
  lock|install)
    if [ -f "$state/$1-result" ]; then
      cp "$state/$1-result" Gemfile.lock
    fi
    if [ -f "$state/$1-status" ]; then
      exit "$(cat "$state/$1-status")"
    fi
    ;;
esac
exit 0
"""

STUB_FAILING_SED = """#!/usr/bin/env bash
echo "stub sed failure" >&2
exit 4
"""

POSIX_CLASSES = {
    "[:alnum:]": "a-zA-Z0-9",
    "[:alpha:]": "a-zA-Z",
    "[:digit:]": "0-9",
    "[:lower:]": "a-z",
    "[:upper:]": "A-Z",
    "[:xdigit:]": "0-9A-Fa-f",
    "[:space:]": " \\t\\n\\r\\f\\v",
    "[:print:]": " -~",
}

# Constructs GNU and macOS BSD `sed -E` / `grep -E` either reject or interpret differently.
NON_PORTABLE_ERE = (
    "\\d", "\\D", "\\s", "\\S", "\\w", "\\W", "\\b", "\\B", "\\<", "\\>",
    "(?", "*?", "+?", "??", "}?", "(|", "|)", "||",
)


def read(path):
    return path.read_text(encoding="utf-8")


def steps(workflow):
    parts = workflow.split("      - name: ")[1:]
    return {part.splitlines()[0]: part for part in parts}


def run_script(step):
    marker = "        run: |\n"
    body = step[step.index(marker) + len(marker):]
    lines = []
    for line in body.split("\n"):
        if line and not line.startswith("          "):
            break
        lines.append(line[10:])
    return "\n".join(lines).rstrip("\n") + "\n"


def find_bash():
    bash = shutil.which("bash")
    if bash and os.name == "nt" and "system32" in bash.lower():
        bash = None
    if bash is None and os.name == "nt":
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidate = Path(program_files) / "Git" / "bin" / "bash.exe"
        if candidate.is_file():
            bash = str(candidate)
    return bash


def write_executable(path, text):
    path.write_bytes(text.encode("utf-8"))
    path.chmod(0o755)


def python_regex(posix_ere):
    for posix_class, python_class in POSIX_CLASSES.items():
        posix_ere = posix_ere.replace(posix_class, python_class)
    return posix_ere


def load_patterns():
    with PATTERNS.open(encoding="utf-8") as handle:
        return json.load(handle)


def bracket_expressions(pattern):
    """Returns the contents of each ERE bracket expression in pattern."""
    found = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            index += 2
            continue
        if char != "[":
            index += 1
            continue
        end = index + 1
        if end < len(pattern) and pattern[end] == "^":
            end += 1
        if end < len(pattern) and pattern[end] == "]":
            end += 1
        while end < len(pattern) and pattern[end] != "]":
            if pattern[end] == "[" and end + 1 < len(pattern) and pattern[end + 1] in ":.=":
                end = pattern.index(pattern[end + 1] + "]", end + 2) + 2
            else:
                end += 1
        found.append(pattern[index + 1:end])
        index = end + 1
    return found


class RubyDependencyWiringTests(unittest.TestCase):
    def setUp(self):
        self.workflow = read(DEPLOY)
        self.steps = steps(self.workflow)
        self.setup_ruby = self.steps["Set up Ruby"]
        self.install = self.steps["Install bundle dependencies"]
        self.script = run_script(self.install)

    def test_setup_ruby_neither_installs_nor_caches_the_bundle(self):
        self.assertIn("uses: ruby/setup-ruby@v1", self.setup_ruby)
        self.assertIn("bundler-cache: false", self.setup_ruby)
        # Without a working-directory, setup-ruby never reads the private Gemfile or lockfile.
        self.assertNotIn("working-directory", self.setup_ruby)
        for path in (BUILD, DEPLOY):
            workflow = read(path)
            with self.subTest(workflow=path.name):
                self.assertNotIn("bundler-cache: true", workflow)
                self.assertNotIn("cache-version", workflow)
                self.assertIsNone(re.search(r"hashFiles\([^)]*Gemfile", workflow))
                # Caches in this public repository are restorable by fork pull-request workflows,
                # so nothing built from the private source (gems, DerivedData) may be cached.
                self.assertNotIn("actions/cache", workflow)

    def test_bundler_output_only_goes_to_a_discarded_file(self):
        self.assertIn("BUNDLE_JOBS: '4'", self.install)
        self.assertIn("BUNDLE_RETRY: '3'", self.install)
        self.assertIn('bundle_log="$RUNNER_TEMP/bundle-install.log"', self.script)
        self.assertIn("trap 'rm -f \"$bundle_log\"' EXIT", self.script)
        bundle_commands = [
            line.strip() for line in self.script.splitlines() if re.match(r"\s*bundle\s", line)
        ]
        self.assertEqual(bundle_commands, ['bundle "$@" >> "$bundle_log" 2>&1 || status=$?'])
        for reader in ("cat ", "tail ", "head ", "less ", "tee ", "::group::", "--verbose"):
            self.assertNotIn(reader, self.script)

    def test_lockfile_guard_runs_before_and_after_install(self):
        lines = [line.strip() for line in self.script.splitlines()]
        install = lines.index("quiet_bundle install install")
        guards = [index for index, line in enumerate(lines) if line == "require_public_sources"]
        self.assertTrue(any(index < install for index in guards))
        self.assertTrue(any(index > install for index in guards))
        self.assertLess(lines.index("quiet_bundle lock lock"), min(guards))


class RubyDependencyInstallBehaviourTests(unittest.TestCase):
    def setUp(self):
        self.bash = find_bash()
        if self.bash is None:
            self.skipTest("bash is not available")
        self.script = run_script(steps(read(DEPLOY))["Install bundle dependencies"])

    def run_install(self, lockfile=None, lock_result=None, install_result=None,
                    lock_status=None, install_status=None):
        with tempfile.TemporaryDirectory(prefix="synthetic-bundle-") as temporary:
            directory = Path(temporary)
            source, state, stub_bin, runner_temp = (
                directory / name for name in ("source", "state", "bin", "runner-temp")
            )
            for folder in (source, state, stub_bin, runner_temp):
                folder.mkdir()
            write_executable(stub_bin / "bundle", STUB_BUNDLE)
            if lockfile is not None:
                (source / "Gemfile.lock").write_bytes(lockfile.encode("utf-8"))
            for name, value in (("lock-result", lock_result), ("install-result", install_result)):
                if value is not None:
                    (state / name).write_bytes(value.encode("utf-8"))
            for name, value in (("lock-status", lock_status), ("install-status", install_status)):
                if value is not None:
                    (state / name).write_bytes(str(value).encode("utf-8"))
            prelude = 'PATH="$(cd ../bin && pwd):$PATH"\nexport PATH\n'
            (directory / "step.sh").write_bytes((prelude + self.script).encode("utf-8"))
            env = os.environ.copy()
            env["RUNNER_TEMP"] = "../runner-temp"
            result = subprocess.run(
                [self.bash, "--noprofile", "--norc", "../step.sh"],
                cwd=source,
                env=env,
                capture_output=True,
            )
            calls_file = state / "calls.log"
            calls = calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else []
            return {
                "status": result.returncode,
                "output": (result.stdout + result.stderr).decode("utf-8", errors="replace"),
                "calls": calls,
                "log_left": (runner_temp / "bundle-install.log").exists(),
            }

    def assert_quiet(self, outcome, *private_text):
        for text in (PRIVATE_GEM, PRIVATE_HOST, "Fetching", "Installing", *private_text):
            self.assertNotIn(text, outcome["output"])
        self.assertFalse(outcome["log_left"])

    def test_clean_lockfile_installs_without_printing_bundler_output(self):
        for name, lockfile in (("lf", CLEAN_LOCKFILE), ("crlf", CLEAN_LOCKFILE.replace("\n", "\r\n"))):
            with self.subTest(line_endings=name):
                outcome = self.run_install(lockfile=lockfile)
                self.assertEqual(outcome["status"], 0, outcome["output"])
                self.assertEqual(
                    outcome["calls"], ["config set --local path vendor/bundle", "install"]
                )
                self.assertIn("Ruby dependencies installed (output suppressed)", outcome["output"])
                self.assert_quiet(outcome)

    def test_disallowed_sources_fail_before_install_without_printing_them(self):
        for case, (lockfile, offending_line) in DISALLOWED_LOCKFILES.items():
            with self.subTest(case=case):
                outcome = self.run_install(lockfile=lockfile)
                self.assertNotEqual(outcome["status"], 0)
                self.assertIn(GUARD_ERROR, outcome["output"])
                self.assertNotIn("install", outcome["calls"])
                self.assert_quiet(outcome, offending_line)

    def test_generated_lockfile_is_checked_before_install(self):
        outcome = self.run_install(lock_result=CLEAN_LOCKFILE)
        self.assertEqual(outcome["status"], 0, outcome["output"])
        self.assertEqual(
            outcome["calls"], ["config set --local path vendor/bundle", "lock", "install"]
        )
        self.assert_quiet(outcome)

        lockfile, offending_line = DISALLOWED_LOCKFILES["git source"]
        outcome = self.run_install(lock_result=lockfile)
        self.assertNotEqual(outcome["status"], 0)
        self.assertIn(GUARD_ERROR, outcome["output"])
        self.assertNotIn("install", outcome["calls"])
        self.assert_quiet(outcome, offending_line)

    def test_lockfile_rewritten_by_install_is_rechecked(self):
        lockfile, offending_line = DISALLOWED_LOCKFILES["path source"]
        outcome = self.run_install(lockfile=CLEAN_LOCKFILE, install_result=lockfile)
        self.assertNotEqual(outcome["status"], 0)
        self.assertIn("install", outcome["calls"])
        self.assertIn(GUARD_ERROR, outcome["output"])
        self.assertNotIn("Ruby dependencies installed", outcome["output"])
        self.assert_quiet(outcome, offending_line)

    def test_bundler_failures_report_only_the_exit_code(self):
        outcome = self.run_install(lockfile=CLEAN_LOCKFILE, install_status=7)
        self.assertEqual(outcome["status"], 1)
        self.assertIn("::error::bundle install failed (exit 7); output suppressed", outcome["output"])
        self.assert_quiet(outcome)

        outcome = self.run_install(lock_status=3)
        self.assertEqual(outcome["status"], 1)
        self.assertIn("::error::bundle lock failed (exit 3); output suppressed", outcome["output"])
        self.assertNotIn("install", outcome["calls"])
        self.assert_quiet(outcome)


class BuildLogSanitizerTests(unittest.TestCase):
    def setUp(self):
        self.workflow = read(BUILD)
        self.steps = steps(self.workflow)
        self.step = self.steps["Sanitize log excerpt"]
        self.script = run_script(self.step)

    def run_sanitizer(self, log_bytes, job_status, stale_excerpt=None, failing_sed=False):
        bash = find_bash()
        if bash is None:
            self.skipTest("bash is not available")
        script = self.script.replace("${{ job.status }}", job_status)
        self.assertNotIn("${{", script)
        with tempfile.TemporaryDirectory(prefix="synthetic-sanitize-") as temporary:
            directory = Path(temporary)
            source = directory / "source"
            source.mkdir()
            (source / "test.log").write_bytes(log_bytes)
            if stale_excerpt is not None:
                (source / "sanitized-log.txt").write_bytes(stale_excerpt)
            bin_dir = directory / "bin"
            bin_dir.mkdir()
            # Run the step's python3 selector with this interpreter, as on the runner.
            write_executable(
                bin_dir / "python3", f'#!/bin/sh\nexec "{Path(sys.executable).as_posix()}" "$@"\n'
            )
            if failing_sed:
                write_executable(bin_dir / "sed", STUB_FAILING_SED)
            prelude = 'PATH="$(cd bin && pwd):$PATH"\nexport PATH\n'
            (directory / "step.sh").write_bytes((prelude + script).encode("utf-8"))
            env = os.environ.copy()
            # Simulate the runner's UTF-8 locale; the step must not depend on it.
            env.update(
                LANG="en_US.UTF-8",
                LC_ALL="en_US.UTF-8",
                MSYS_NO_PATHCONV="1",
                SCHEME_NAME="SyntheticScheme",
                BUNDLE_ID="com.example.synthetic",
                TEAM_ID="SYNTHTEAM1",
            )
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "step.sh"],
                cwd=directory,
                env=env,
                capture_output=True,
            )
            excerpt = source / "sanitized-log.txt"
            return {
                "status": result.returncode,
                "output": result.stdout + result.stderr,
                "excerpt": excerpt.read_bytes() if excerpt.exists() else None,
                "tmp_left": (source / "sanitized-log.txt.tmp").exists(),
            }

    def test_raw_text_commands_run_in_c_locale(self):
        commands = []
        for line in self.script.splitlines():
            if line.lstrip().startswith("#"):
                continue
            for match in re.finditer(r"(?<![\w-])(grep|sed|awk|tr)(?= )", line):
                commands.append(match.group(1))
                self.assertTrue(
                    line[:match.start()].endswith("LC_ALL=C "),
                    f"{match.group(1)} runs without LC_ALL=C: {line.strip()}",
                )
        self.assertEqual(sorted(commands), ["awk", "awk", "grep", "grep", "sed", "sed", "tr"])

    def test_sanitizer_failure_withholds_excerpt_without_failing_build(self):
        self.assertIn("\n        continue-on-error: true\n", self.step)
        removal = self.script.index('rm -f "$sanitized_log_path" "$sanitized_tmp_path"')
        self.assertLess(removal, self.script.index("LC_ALL=C grep -E"))
        self.assertLess(
            self.script.index('printf \'%s\' "$sanitized" > "$sanitized_tmp_path"'),
            self.script.index('mv -f "$sanitized_tmp_path" "$sanitized_log_path"'),
        )
        self.assertNotIn('printf \'%s\' "$sanitized" > "$sanitized_log_path"', self.script)

    def test_non_utf8_bytes_do_not_stop_path_redaction(self):
        outcome = self.run_sanitizer(
            b"\x1b[1;31merror: /Users/runner/work/synthetic/Sources\xff\xfeApp/Main.swift:10:5:"
            b" synthetic failure\x1b[0m\n"
            b"Test Suite 'SyntheticSchemeTests' failed at 2026-10-13 01:51:03.820.\n"
            b"\xe2\x9c\x98 Test example() recorded an issue for com.example.synthetic SYNTHTEAM1\n"
            b"Executed 3 tests, with 1 failure (0 unexpected) in 0.010 (0.012) seconds\n",
            "failure",
        )
        self.assertEqual(outcome["status"], 0, outcome["output"])
        excerpt = outcome["excerpt"]
        self.assertIsNotNone(excerpt)
        for private in (b"/Users/", b"Sources", b"\xff", b"Main.swift", b"SyntheticScheme",
                        b"com.example.synthetic", b"SYNTHTEAM1"):
            self.assertNotIn(private, excerpt)
        self.assertIn(b"error: <path> synthetic failure", excerpt)
        self.assertIn(b"\xe2\x9c\x98 Test example() recorded an issue for <bundle-id> <team-id>", excerpt)
        self.assertIn(b"Test Suite '[SCHEME]Tests' failed", excerpt)
        self.assertIn(b"Executed 3 tests, with 1 failure", excerpt)
        self.assertFalse(outcome["tmp_left"])
        self.assertEqual(outcome["output"], b"")

    def test_long_excerpt_is_truncated_without_a_pipe_failure(self):
        lines = b"".join(
            b"\xe2\x9c\x94 Test case%05d() passed after 0.001 seconds.\n" % number
            for number in range(3000)
        )
        outcome = self.run_sanitizer(lines, "success")
        self.assertEqual(outcome["status"], 0, outcome["output"])
        excerpt = outcome["excerpt"]
        self.assertTrue(excerpt.startswith(b"\xe2\x9c\x94 Test case00000() passed"))
        self.assertLessEqual(len(excerpt), 8192)
        self.assertLessEqual(len(excerpt.split(b"\n")), 60)

    def test_filter_failure_removes_stale_excerpt_and_prints_no_log_text(self):
        outcome = self.run_sanitizer(
            b"error: SYNTHETIC-RAW-LINE /Users/runner/private\n",
            "failure",
            stale_excerpt=b"SYNTHETIC-STALE-EXCERPT",
            failing_sed=True,
        )
        self.assertNotEqual(outcome["status"], 0)
        self.assertIsNone(outcome["excerpt"])
        self.assertFalse(outcome["tmp_left"])
        self.assertNotIn(b"SYNTHETIC-RAW-LINE", outcome["output"])
        self.assertNotIn(b"SYNTHETIC-STALE-EXCERPT", outcome["output"])

    def test_missing_checkout_leaves_a_placeholder_excerpt(self):
        bash = find_bash()
        if bash is None:
            self.skipTest("bash is not available")
        script = self.script.replace("${{ job.status }}", "failure")
        with tempfile.TemporaryDirectory(prefix="synthetic-sanitize-") as temporary:
            directory = Path(temporary)
            (directory / "step.sh").write_bytes(script.encode("utf-8"))
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "step.sh"],
                cwd=directory,
                capture_output=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            self.assertEqual(
                (directory / "source" / "sanitized-log.txt").read_bytes(),
                b"No log: source was not cloned",
            )

    def test_comment_steps_tolerate_non_utf8_excerpts(self):
        for name in ("Comment on merge request success", "Comment on merge request failure"):
            script = run_script(self.steps[name])
            with self.subTest(step=name):
                self.assertIn('read_text(encoding="utf-8", errors="replace")', script)
                self.assertNotIn('read_text(encoding="utf-8")', script)
                program = script[script.index("python3 - <<'PY'\n") + len("python3 - <<'PY'\n"):]
                program = program[:program.index("\nPY\n")]
                with tempfile.TemporaryDirectory(prefix="synthetic-comment-") as temporary:
                    directory = Path(temporary)
                    excerpt = b"Executed 1 test \xff\xfe ok \xe2\x9c"
                    (directory / "sanitized-log.txt").write_bytes(excerpt)
                    body = directory / "body.md"
                    env = os.environ.copy()
                    env.update(RUN_URL="https://example.invalid/run", BODY_FILE=str(body))
                    # factory-4's comment steps run with working-directory: source.
                    self.assertIn("        working-directory: source\n", self.steps[name])
                    result = subprocess.run(
                        [sys.executable, "-"],
                        input=program.encode("utf-8"),
                        cwd=directory,
                        env=env,
                        capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
                    self.assertIn("Executed 1 test", body.read_text(encoding="utf-8"))


class SanitizePatternsTests(unittest.TestCase):
    def setUp(self):
        self.config = load_patterns()
        self.patterns = self.config["static_patterns"]

    def test_config_is_well_formed(self):
        self.assertEqual(
            set(self.config), {"static_patterns", "dynamic_env_vars", "post_processing"}
        )
        self.assertGreater(len(self.patterns), 0)
        for entry in self.patterns:
            with self.subTest(comment=entry.get("comment")):
                self.assertEqual(set(entry), {"match", "replace", "comment"})
                for key in ("match", "replace", "comment"):
                    self.assertIsInstance(entry[key], str)
                    self.assertTrue(entry[key])
                # The workflow reads match<TAB>replace lines.
                for key in ("match", "replace"):
                    self.assertNotIn("\t", entry[key])
                    self.assertNotIn("\n", entry[key])
        for name in self.config["dynamic_env_vars"]:
            self.assertRegex(name, r"^[A-Z][A-Z0-9_]*$")
        post = self.config["post_processing"]
        self.assertIsInstance(post["max_lines"], int)
        self.assertIsInstance(post["max_bytes"], int)

    def test_patterns_compile_as_portable_ere(self):
        for entry in self.patterns:
            match = entry["match"]
            with self.subTest(comment=entry["comment"]):
                for token in NON_PORTABLE_ERE:
                    self.assertNotIn(token, match)
                compiled = re.compile(python_regex(match))
                for reference in re.findall(r"\\(\d)", entry["replace"]):
                    self.assertLessEqual(int(reference), compiled.groups)

    def test_patterns_compile_in_sed_as_the_workflow_applies_them(self):
        bash = find_bash()
        if bash is None:
            self.skipTest("bash is not available")
        deploy = read(DEPLOY)
        self.assertIn('match="${match//#/\\\\#}"', deploy)
        self.assertIn('LC_ALL=C sed -i.bak -E "s#${match}#${replace}#g" "$temp_log"', deploy)
        script = (
            "set -uo pipefail\n"
            "status=0\n"
            "while IFS=$'\\t' read -r match replace; do\n"
            '  match="${match//#/\\\\#}"\n'
            '  replace="${replace//#/\\\\#}"\n'
            '  if ! LC_ALL=C sed -E "s#${match}#${replace}#g" sample.log > /dev/null; then\n'
            "    printf 'FAILED %s\\n' \"$match\"\n"
            "    status=1\n"
            "  fi\n"
            "done < patterns.tsv\n"
            'exit "$status"\n'
        )
        with tempfile.TemporaryDirectory(prefix="synthetic-patterns-") as temporary:
            directory = Path(temporary)
            (directory / "patterns.tsv").write_bytes(
                "".join(f"{entry['match']}\t{entry['replace']}\n" for entry in self.patterns).encode("utf-8")
            )
            (directory / "sample.log").write_bytes(b"synthetic line \xff /Users/runner/x\n")
            (directory / "check.sh").write_bytes(script.encode("utf-8"))
            result = subprocess.run(
                [bash, "--noprofile", "--norc", "check.sh"],
                cwd=directory,
                capture_output=True,
            )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        self.assertEqual(result.returncode, 0, output)
        self.assertNotIn("FAILED", output)

    def test_dependency_hashes_are_already_redacted(self):
        def sanitize(text):
            for entry in self.patterns:
                text = re.sub(python_regex(entry["match"]), entry["replace"], text)
            return text

        lockfile_hash = "0f" * 32
        cache_key = (
            "setup-ruby-bundler-cache-v6-macos-arm64-ruby-3.2.0-wd-source-with--without--only--"
            "Gemfile.lock-" + lockfile_hash
        )
        self.assertNotIn(lockfile_hash, sanitize(cache_key))
        git_revision = "0123456789abcdef" * 2 + "01234567"
        self.assertNotIn(git_revision, sanitize("  revision: " + git_revision))

    def test_bracket_expressions_are_portable(self):
        # Inside a POSIX bracket a backslash is literal, so "[^)\\n]" excludes "\" and "n"
        # on BSD sed rather than newline, and "/-=" is an unintended range.
        brackets = [
            (entry["comment"], content)
            for entry in self.patterns
            for content in bracket_expressions(entry["match"])
        ]
        self.assertGreater(len(brackets), 0)
        for comment, content in brackets:
            with self.subTest(comment=comment, bracket=content):
                self.assertIsNone(re.search(r"\\[A-Za-z]", content))
                self.assertNotIn("/-=", content)

    def test_credentialed_urls_are_redacted_before_emails(self):
        comments = [entry["comment"] for entry in self.patterns]
        credentialed = comments.index("Git or API URLs containing embedded credentials")
        email = next(i for i, comment in enumerate(comments) if comment.startswith("Email addresses"))
        self.assertLess(credentialed, email)

        def sanitize(text):
            for entry in self.patterns:
                text = re.sub(python_regex(entry["match"]), entry["replace"], text)
            return text

        redacted = sanitize("clone https://synthetic-user:hunter2@git.example.invalid/x.git")
        self.assertIn("<credentialed-url>", redacted)
        self.assertNotIn("hunter2", redacted)

    def test_bracket_parser_finds_the_known_bug_shapes(self):
        self.assertEqual(bracket_expressions('a[^")\\n]*b'), ['^")\\n'])
        self.assertEqual(bracket_expressions("[A-Za-z0-9._~+/-=]+"), ["A-Za-z0-9._~+/-="])
        self.assertEqual(bracket_expressions("[^/@[:space:]]+\\[x"), ["^/@[:space:]"])


if __name__ == "__main__":
    unittest.main()
