import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github" / "workflows" / "build-and-test.yml"
STEP_NAME = "Sanitize log excerpt"

SCHEME = "SecretScheme"
BUNDLE = "com.example.secretbundle"
TEAM = "TEAM123456"
UUID = "0123ABCD-4567-89EF-0123-456789ABCDEF"
# The runner checks the source repository out into $GITHUB_WORKSPACE/source.
SOURCE = "/Users/runner/work/synthetic-proxy/synthetic-proxy/source"
TESTS_PATH = f"{SOURCE}/{SCHEME}Tests"
TRUNCATED = re.compile(r"^\.\.\. ([0-9]+) more lines not shown$")


def step_script():
    lines = WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = lines.index(f"      - name: {STEP_NAME}")
    run = next(i for i in range(start, len(lines)) if lines[i] == "        run: |")
    body = []
    for line in lines[run + 1:]:
        if line.strip() and not line.startswith(" " * 10):
            break
        body.append(line[10:])
    return "\n".join(body).rstrip() + "\n"


def embedded_python():
    return re.search(r"<<'PY'\n(.*?)\nPY\n", step_script(), re.S).group(1)


def find_bash():
    if os.name != "nt":
        return shutil.which("bash")
    # Avoid System32\bash.exe (WSL); use Git for Windows' bash.
    candidates = []
    git = shutil.which("git")
    if git:
        for parent in Path(git).resolve().parents[:3]:
            candidates += [parent / "bin" / "bash.exe", parent / "usr" / "bin" / "bash.exe"]
    candidates.append(
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Git" / "bin" / "bash.exe"
    )
    return next((str(path) for path in candidates if path.is_file()), None)


BASH = find_bash()


def swift_passes(count, prefix="passes"):
    lines = []
    for index in range(count):
        lines.append(f'\u25c7 Test "{prefix} {index}" started.')
        lines.append(f'\u2714 Test "{prefix} {index}" passed after 0.001 seconds.')
    return lines


XCTEST_EMPTY_SUMMARY = [
    "Test Suite 'All tests' started at 2026-10-13 10:00:00.000.",
    "Test Suite 'All tests' passed at 2026-10-13 10:00:00.001.",
    "\t Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds",
]

SWIFT_RUN_START = [
    "\u25c7 Test run started.",
    "\u21b3 Testing Library Version: 6.1 (arm64-apple-ios13.0-simulator)",
    '\u25c7 Suite "DepartureParserTests" started.',
]


@unittest.skipUnless(BASH, "bash is not available")
class SanitizeLogExcerptTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sanitize-log-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.shims = self.directory / "shims"
        self.shims.mkdir()
        self.use_python(True)

    def use_python(self, available):
        python = Path(sys.executable).as_posix()
        body = f'exec "{python}" "$@"\n' if available else "exit 127\n"
        shim = self.shims / "python3"
        shim.write_bytes(("#!/bin/sh\n" + body).encode("utf-8"))
        shim.chmod(0o755)

    def run_step(self, status, logs, *, source=True):
        work = self.directory / "work"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir()
        if source:
            (work / "source").mkdir()
            for name, lines in logs.items():
                (work / "source" / name).write_bytes("\n".join(lines).encode("utf-8") + b"\n")
        script = step_script()
        self.assertEqual(script.count("${{ job.status }}"), 1)
        script = script.replace("${{ job.status }}", status)
        self.assertNotIn("${{", script)
        (work / "step.sh").write_bytes(script.encode("utf-8"))
        environment = {
            key: value
            for key, value in os.environ.items()
            if key not in {"SCHEME_NAME", "BUNDLE_ID", "TEAM_ID"}
        }
        environment.update({
            "SCHEME_NAME": SCHEME,
            "BUNDLE_ID": BUNDLE,
            "TEAM_ID": TEAM,
            # Simulate the runner's UTF-8 locale; the step must not depend on it.
            "LANG": "en_US.UTF-8",
            "LC_ALL": "en_US.UTF-8",
            "MSYS_NO_PATHCONV": "1",
            "PATH": str(self.shims) + os.pathsep + environment.get("PATH", ""),
        })
        result = subprocess.run(
            [BASH, "--noprofile", "--norc", "-eo", "pipefail", "step.sh"],
            cwd=work,
            env=environment,
            capture_output=True,
            check=False,
            timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        # The excerpt must never reach the public job log.
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self.assertFalse((work / "source" / "sanitized-log.txt.tmp").exists())
        output = (work / "source" / "sanitized-log.txt").read_bytes()
        self.assertLessEqual(len(output), 8192)
        text = output.decode("utf-8")
        self.assertLessEqual(len(text.splitlines()), 60)
        return text

    def assert_sanitized(self, text):
        for value in (SCHEME, BUNDLE, TEAM, UUID, "/Users/", "synthetic-proxy", "`",
                      "__SANITIZED_EOF__"):
            self.assertNotIn(value, text)
        self.assertNotRegex(text, r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-")

    def test_compile_error_keeps_location_and_source_context(self):
        view = f"{SOURCE}/{SCHEME}/Views/ExampleView.swift"
        target = f"(in target '{SCHEME}' from project '{SCHEME}')"
        source_line = "        let value = make()"
        caret = "                      ^"
        log = [
            "--- xcodebuild: WARNING: Using the first of multiple matching destinations:",
            f"{{ platform:iOS Simulator, arch:arm64, id:{UUID}, OS:18.5, name:iPhone 16 }}",
            "",
            f"SwiftCompile normal arm64 {view} {target}",
            f"    cd {SOURCE}",
            "    builtin-swiftTaskExecution -- /Applications/Xcode_16.4.app/Contents/Developer"
            f"/usr/bin/swift-frontend -frontend -c -module-name {SCHEME}",
            "",
            f"{view}:42:17: error: generic parameter 'T' could not be inferred",
            source_line,
            caret,
            f"{view}:42:17: note: explicitly specify the generic arguments to fix this issue",
            source_line,
            caret,
            "                             <<#T#>>",
            "",
            f"SwiftEmitModule normal arm64 Emitting\\ module\\ for\\ {SCHEME} {target}",
            "",
            # The module emitter repeats the same diagnostic; it is listed once.
            f"{view}:42:17: error: generic parameter 'T' could not be inferred",
            source_line,
            caret,
            "",
            "** BUILD FAILED **",
            "",
            "",
            "The following build commands failed:",
            f"\tSwiftCompile normal arm64 Compiling\\ ExampleView.swift {view} {target}",
            f"\tSwiftEmitModule normal arm64 Emitting\\ module\\ for\\ {SCHEME} {target}",
            "(2 failures)",
        ]
        text = self.run_step("failure", {"build.log": log})
        relative = "[SCHEME]/Views/ExampleView.swift"
        sanitized_target = "(in target '[SCHEME]' from project '[SCHEME]')"
        self.assertEqual(
            text.splitlines(),
            [
                f"{relative}:42:17: error: generic parameter 'T' could not be inferred",
                source_line,
                caret,
                f"{relative}:42:17: note: explicitly specify the generic arguments to fix this issue",
                source_line,
                caret,
                "                             <<#T#>>",
                "** BUILD FAILED **",
                "The following build commands failed:",
                f"\tSwiftCompile normal arm64 Compiling\\ ExampleView.swift {relative} "
                f"{sanitized_target}",
                f"\tSwiftEmitModule normal arm64 Emitting\\ module\\ for\\ [SCHEME] "
                f"{sanitized_target}",
                "(2 failures)",
            ],
        )
        self.assert_sanitized(text)

    def test_source_context_is_capped_and_ends_at_the_next_diagnostic(self):
        board = f"{SOURCE}/{SCHEME}/Board.swift"
        log = [
            f"{board}:10:5: error: cannot find 'departures' in scope",
        ] + [f"    context {index}" for index in range(10)] + [
            f"{board}:20:5: error: cannot find 'arrivals' in scope",
            "    arrivals.removeAll()",
            f"{board}:21:5: warning: variable 'unused' was never used",
            "    let unused = 1",
            f"{board}:30:1: error: expected '}}' in class",
            "",
            "    after a blank line",
        ]
        text = self.run_step("failure", {"build.log": log})
        self.assertEqual(
            text.splitlines(),
            [f"[SCHEME]/Board.swift:10:5: error: cannot find 'departures' in scope"]
            + [f"    context {index}" for index in range(6)]
            + [
                "[SCHEME]/Board.swift:20:5: error: cannot find 'arrivals' in scope",
                "    arrivals.removeAll()",
                "[SCHEME]/Board.swift:30:1: error: expected '}' in class",
            ],
        )

    def test_paths_outside_the_checkout_are_still_redacted(self):
        log = [
            "/Users/runner/Library/Developer/Xcode/DerivedData/App-abc/SourcePackages/checkouts"
            "/SomePackage/Sources/Lib/File.swift:7:3: error: synthetic package failure",
            "/Users/runner/work/synthetic-proxy/synthetic-proxy/other/File.swift:1:1: error: other",
            "/private/var/folders/xy/T/File.swift:2:2: error: temporary",
            "** BUILD FAILED **",
        ]
        text = self.run_step("failure", {"build.log": log})
        self.assertEqual(
            text.splitlines(),
            [
                "<path> error: synthetic package failure",
                "<path> error: other",
                "<path> error: temporary",
                "** BUILD FAILED **",
            ],
        )
        self.assert_sanitized(text)

    def test_failing_swift_tests_are_listed_before_passing_noise(self):
        log = (
            [f"Command line invocation: xcodebuild test -scheme {SCHEME}"]
            + XCTEST_EMPTY_SUMMARY
            + SWIFT_RUN_START
            + swift_passes(60, "early")
            + ['\u2714 Test "shows error: offline banner" passed after 0.001 seconds.']
            + [
                '\u25c7 Test "parses departures" started.',
                '\u2718 Test "parses departures" recorded an issue at '
                "DepartureParserTests.swift:42:9: Expectation failed: "
                "(departures.count \u2192 2) == 3",
                "\u21b3 departures.count: 2",
                f"\u21b3 fixture '{BUNDLE}' for team {TEAM} on {UUID} `stale`",
                '\u2718 Test "parses departures" failed after 0.004 seconds with 1 issue.',
                '\u2718 Suite "DepartureParserTests" failed after 0.010 seconds with 1 issue.',
            ]
            + swift_passes(60, "late")
            + [
                '\u2718 Test "renders __SANITIZED_EOF__ marker" recorded an issue at '
                "MarkerTests.swift:7:5: Expectation failed: marker.isEmpty",
                '\u2718 Test "renders __SANITIZED_EOF__ marker" failed after 0.002 seconds '
                "with 1 issue.",
                f"{TESTS_PATH}/LegacyTests.swift:12: error: -[{SCHEME}Tests.LegacyTests "
                'testLegacy] : XCTAssertEqual failed: ("1") is not equal to ("2")',
                f"Test Case '-[{SCHEME}Tests.LegacyTests testLegacy]' failed (0.002 seconds).",
                # XCTest failures have no column, so the next lines are not taken as context.
                f"Test Case '-[{SCHEME}Tests.LegacyTests testOther]' started.",
                "\u2718 Test run with 123 tests failed after 1.234 seconds with 3 issues.",
                "",
                "Failing tests:",
                f"\t{SCHEME}Tests/DepartureParserTests/parsesDepartures()",
                f"\t{SCHEME}Tests/LegacyTests/testLegacy()",
                "",
                "** TEST FAILED **",
            ]
        )
        text = self.run_step("failure", {"test.log": log})
        expected = [
            '\u2718 Test "parses departures" recorded an issue at '
            "DepartureParserTests.swift:42:9: Expectation failed: (departures.count \u2192 2) == 3",
            "\u21b3 departures.count: 2",
            "\u21b3 fixture '<bundle-id>' for team <team-id> on <uuid> 'stale'",
            '\u2718 Test "parses departures" failed after 0.004 seconds with 1 issue.',
            '\u2718 Suite "DepartureParserTests" failed after 0.010 seconds with 1 issue.',
            '\u2718 Test "renders [REDACTED] marker" recorded an issue at '
            "MarkerTests.swift:7:5: Expectation failed: marker.isEmpty",
            '\u2718 Test "renders [REDACTED] marker" failed after 0.002 seconds with 1 issue.',
            "[SCHEME]Tests/LegacyTests.swift:12: error: -[[SCHEME]Tests.LegacyTests testLegacy] : "
            'XCTAssertEqual failed: ("1") is not equal to ("2")',
            "Test Case '-[[SCHEME]Tests.LegacyTests testLegacy]' failed (0.002 seconds).",
            "\u2718 Test run with 123 tests failed after 1.234 seconds with 3 issues.",
            "Failing tests:",
            "\t[SCHEME]Tests/DepartureParserTests/parsesDepartures()",
            "\t[SCHEME]Tests/LegacyTests/testLegacy()",
            "** TEST FAILED **",
            "Test Suite 'All tests' started at 2026-10-13 10:00:00.000.",
            "Test Suite 'All tests' passed at 2026-10-13 10:00:00.001.",
            "\t Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds",
        ]
        self.assertEqual(text.splitlines(), expected)
        self.assert_sanitized(text)

    def test_crash_reports_tests_that_never_finished(self):
        log = (
            XCTEST_EMPTY_SUMMARY
            + SWIFT_RUN_START
            + swift_passes(80)
            + [
                f"    CompileSwift normal arm64 {SOURCE}/{SCHEME}/CrashReporterBridge.swift",
                '\u25c7 Test "decodes timetable" started.',
                f"{TESTS_PATH}/TimetableDecoder.swift:88: Fatal error: "
                "Unexpectedly found nil while unwrapping an Optional value",
                "Restarting after unexpected exit, crash, or test timeout in "
                "TimetableTests/decodes(); summary will include totals from previous launches.",
                f"{SCHEME} (4242) encountered an error (Early unexpected exit, operation "
                "never finished bootstrapping - no restart will be attempted.)",
                "Test crashed with signal segv.",
                "",
                "** TEST FAILED **",
            ]
        )
        text = self.run_step("failure", {"test.log": log})
        self.assertEqual(
            text.splitlines(),
            [
                "[SCHEME]Tests/TimetableDecoder.swift:88: Fatal error: "
                "Unexpectedly found nil while unwrapping an Optional value",
                "Restarting after unexpected exit, crash, or test timeout in "
                "TimetableTests/decodes(); summary will include totals from previous launches.",
                "[SCHEME] (4242) encountered an error (Early unexpected exit, operation "
                "never finished bootstrapping - no restart will be attempted.)",
                "Test crashed with signal segv.",
                "** TEST FAILED **",
                "Tests started but not finished (possible crash or hang):",
                '\u25c7 Test "decodes timetable" started.',
                "Test Suite 'All tests' started at 2026-10-13 10:00:00.000.",
                "Test Suite 'All tests' passed at 2026-10-13 10:00:00.001.",
                "\t Executed 0 tests, with 0 failures (0 unexpected) in 0.000 (0.001) seconds",
            ],
        )
        self.assert_sanitized(text)

    def test_issue_details_are_capped_and_pass_details_are_dropped(self):
        log = SWIFT_RUN_START + [
            '\u2718 Test "logs" recorded an issue at LogTests.swift:9:5: Expectation failed',
            '    {"apiKey": "dummy-value"}',
            '\u2718 Test "diffs" recorded an issue at DiffTests.swift:3:5: Expectation failed',
        ] + [f"\u00b1 difference {index}" for index in range(20)] + [
            '\u2714 Test "other" passed after 0.001 seconds.',
            "\u21b3 detail of a passing test",
            '\u2718 Test "diffs" failed after 0.001 seconds with 1 issue.',
            "\u2718 Test run with 2 tests failed after 0.002 seconds with 1 issue.",
        ]
        lines = self.run_step("failure", {"test.log": log}).splitlines()
        self.assertEqual(lines[2:7], [f"\u00b1 difference {index}" for index in range(5)])
        self.assertNotIn("\u00b1 difference 5", lines)
        self.assertNotIn('    {"apiKey": "dummy-value"}', lines)
        self.assertNotIn("\u21b3 detail of a passing test", lines)
        self.assertNotIn("Tests started but not finished (possible crash or hang):", lines)
        self.assertIn('\u2718 Test "diffs" failed after 0.001 seconds with 1 issue.', lines)

    def test_long_failures_are_cut_on_whole_lines_and_the_cut_is_reported(self):
        failures = [
            f'\u2718 Test "t{index:02}" recorded an issue at T.swift:{index}:1: '
            "Expectation failed: " + "\u2192" * 30
            for index in range(80)
        ]
        text = self.run_step("failure", {"test.log": failures + ["** TEST FAILED **"]})
        lines = text.splitlines()
        self.assertLess(len(lines), 60)
        self.assertEqual(lines[:-1], failures[:len(lines) - 1])
        hidden = TRUNCATED.match(lines[-1])
        self.assertIsNotNone(hidden, lines[-1])
        self.assertEqual(int(hidden.group(1)), len(failures) + 1 - (len(lines) - 1))

    def test_line_cap_reports_hidden_lines(self):
        board = f"{SOURCE}/{SCHEME}/Board.swift"
        errors = [f"{board}:{index}:1: error: e{index}" for index in range(1, 71)]
        lines = self.run_step("failure", {"build.log": errors + ["** BUILD FAILED **"]}).splitlines()
        self.assertEqual(len(lines), 60)
        self.assertEqual(lines[:59], [f"[SCHEME]/Board.swift:{i}:1: error: e{i}" for i in range(1, 60)])
        self.assertEqual(lines[59], "... 12 more lines not shown")

    def test_one_huge_line_does_not_stall_the_selector(self):
        # "Test .*failed" is quadratic on a long line of "Test " with no "failed";
        # unbounded, 2 MB takes minutes, past this run's timeout.
        log = [
            "Test " * 400_000,
            f"{SOURCE}/{SCHEME}/Board.swift:10:5: error: cannot find 'departures' in scope",
            "",
            "** BUILD FAILED **",
        ]
        text = self.run_step("failure", {"build.log": log})
        self.assertEqual(
            text.splitlines(),
            [
                "[SCHEME]/Board.swift:10:5: error: cannot find 'departures' in scope",
                "** BUILD FAILED **",
            ],
        )

    def test_build_failure_without_test_log_uses_build_log(self):
        log = [
            f"CompileSwift normal arm64 {SOURCE}/{SCHEME}/Board.swift",
            f"{SOURCE}/{SCHEME}/Board.swift:10:5: error: cannot find 'departures' in scope",
            "",
            "Ld /Users/runner/Library/Developer/Xcode/DerivedData/App/Build/App normal",
            "ld: symbol(s) not found for architecture arm64",
            "clang: error: linker command failed with exit code 1",
            "** BUILD FAILED **",
        ]
        text = self.run_step("failure", {"build.log": log})
        self.assertEqual(
            text.splitlines(),
            [
                "[SCHEME]/Board.swift:10:5: error: cannot find 'departures' in scope",
                "ld: symbol(s) not found for architecture arm64",
                "clang: error: linker command failed with exit code 1",
                "** BUILD FAILED **",
            ],
        )
        self.assert_sanitized(text)

    def test_success_excerpt_is_unchanged(self):
        log = (
            XCTEST_EMPTY_SUMMARY
            + SWIFT_RUN_START
            + swift_passes(3)
            + [
                f'\u2714 Suite "{SCHEME}Tests" passed after 0.010 seconds.',
                "\u2714 Test run with 3 tests passed after 0.020 seconds.",
                "** TEST SUCCEEDED **",
            ]
        )
        text = self.run_step("success", {"test.log": log})
        self.assertEqual(
            text.splitlines(),
            XCTEST_EMPTY_SUMMARY
            + [f'\u2714 Test "passes {index}" passed after 0.001 seconds.' for index in range(3)]
            + [
                '\u2714 Suite "[SCHEME]Tests" passed after 0.010 seconds.',
                "\u2714 Test run with 3 tests passed after 0.020 seconds.",
            ],
        )
        many = self.run_step("success", {"test.log": swift_passes(100)}).splitlines()
        self.assertEqual(
            many, [f'\u2714 Test "passes {index}" passed after 0.001 seconds.' for index in range(60)]
        )

    def test_failure_falls_back_to_grep_when_python_is_unavailable(self):
        self.use_python(False)
        log = swift_passes(2) + [
            "--- xcodebuild: WARNING: Using the first of multiple matching destinations:",
            '\u2718 Test "x" failed after 0.001 seconds with 1 issue.',
            f"{SOURCE}/{SCHEME}/Board.swift:10:5: error: cannot find 'departures' in scope",
            "ld: symbol(s) not found for architecture arm64",
            "** TEST FAILED **",
        ]
        text = self.run_step("failure", {"test.log": log})
        self.assertEqual(
            text.splitlines(),
            [
                "[SCHEME]/Board.swift:10:5: error: cannot find 'departures' in scope",
                "ld: symbol(s) not found for architecture arm64",
                "** TEST FAILED **",
                '\u2714 Test "passes 0" passed after 0.001 seconds.',
                '\u2714 Test "passes 1" passed after 0.001 seconds.',
                '\u2718 Test "x" failed after 0.001 seconds with 1 issue.',
            ],
        )

    def test_missing_logs_are_reported(self):
        self.assertEqual(self.run_step("failure", {}), "No log file found")
        self.assertEqual(
            self.run_step("failure", {}, source=False), "No log: source was not cloned"
        )
        self.assertEqual(
            self.run_step("failure", {"test.log": ["nothing relevant"]}),
            "(no matching log lines captured)",
        )


class SanitizeStepWiringTests(unittest.TestCase):
    def test_embedded_python_is_ascii_and_quiet(self):
        # Keeps the heredoc independent of the runner's locale and source decoding.
        self.assertTrue(embedded_python().isascii())
        self.assertIn("2>/dev/null <<'PY'", step_script())

    def test_embedded_python_survives_bash_3_2_command_substitution(self):
        # bash 3.2 scans a heredoc inside $(...) for quotes and parentheses.
        python = embedded_python()
        self.assertNotIn("'", python)
        self.assertEqual(python.count("("), python.count(")"))
        self.assertEqual(python.count('"') % 2, 0)

    def test_linker_pattern_is_anchored(self):
        script = step_script()
        self.assertIn("|(^| )ld:|", script)
        self.assertNotIn("|ld:|", script)


if __name__ == "__main__":
    unittest.main()
