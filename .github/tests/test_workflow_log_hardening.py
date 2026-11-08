import base64
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile


ROOT = Path(__file__).resolve().parents[2]
GITHUB_DIR = ROOT / ".github"
WORKFLOWS = GITHUB_DIR / "workflows"
BUILD = WORKFLOWS / "build-and-test.yml"
DEPLOY = WORKFLOWS / "deploy-testflight.yml"
ANSI_STRIP = "LC_ALL=C sed -E $'s/\\033\\\\[[0-?]*[ -/]*[@-~]//g'"
EXCERPT_FILE = 'excerpt_file="$RUNNER_TEMP/deploy-log-excerpt.txt"'
METADATA_FILE = 'metadata_file="$RUNNER_TEMP/deploy-metadata.json"'

# Synthetic stand-ins for private configuration; none of these may reach step output.
STATE_REPO = "synthetic-owner/synthetic-state-store"
PROXY_PAT = "synthetic-proxy-pat-value"
JOB_ID = "0123abcd-4567-89ab-cdef-0123456789ab"
PRIVATE_BODY = "SYNTHETIC-PRIVATE-BODY"

# Serves canned responses from $STUB_STATE and records every call.
STUB_CURL = r"""#!/usr/bin/env bash
set -u
state="$STUB_STATE"
out=""
fmt=""
method="GET"
url=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o) out="$2"; shift 2 ;;
    -w) fmt="$2"; shift 2 ;;
    -X) method="$2"; shift 2 ;;
    -H|-d|--retry|--retry-delay|--max-time) shift 2 ;;
    https://*) url="$1"; shift ;;
    *) shift ;;
  esac
done
printf '%s %s\n' "$method" "$url" >> "$state/calls.log"
case "$method:$url" in
  DELETE:*) kind=delete ;;
  */contents/) kind=probe ;;
  *) kind=get ;;
esac
status="$(cat "$state/$kind-status" 2>/dev/null || echo 200)"
if [ -f "$state/$kind-body" ]; then
  if [ -z "$out" ]; then
    cat "$state/$kind-body"
  elif [ "$out" != /dev/null ]; then
    cp "$state/$kind-body" "$out"
  fi
fi
token='%{http_code}'
if [ -n "$fmt" ]; then
  printf '%b' "${fmt//"$token"/$status}"
fi
exit "$(cat "$state/$kind-rc" 2>/dev/null || echo 0)"
"""


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


def write_jq_shim(bin_dir):
    jq = shutil.which("jq")
    if jq is None:
        return False
    # A native Windows jq writes CRLF line endings unless --binary is given.
    flag = " -b" if os.name == "nt" else ""
    write_executable(bin_dir / "jq", f'#!/usr/bin/env bash\nexec "{Path(jq).as_posix()}"{flag} "$@"\n')
    return True


PATH_PRELUDE = 'PATH="$(cd "$STUB_BIN" && pwd):$PATH"\nexport PATH\n'
# macOS runners execute run scripts with /bin/bash 3.2; emulate its quoting rules.
MACOS_BASH = {"BASH_COMPAT": "3.2"}


def parse_env_file(text):
    values = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        if "<<" in line:
            name, delimiter = line.split("<<", 1)
            end = lines.index(delimiter, index + 1)
            values[name] = "\n".join(lines[index + 1:end])
            index = end + 1
        elif "=" in line:
            name, value = line.split("=", 1)
            values[name] = value
            index += 1
        else:
            index += 1
    return values


class DispatchStateRepositoryTests(unittest.TestCase):
    def test_state_repository_comes_from_secret(self):
        for path in (BUILD, DEPLOY):
            workflow = read(path)
            with self.subTest(workflow=path.name):
                parse = steps(workflow)["Parse dispatch payload"]
                self.assertIn(
                    "          DISPATCH_STATE_REPO: ${{ secrets.DISPATCH_STATE_REPO }}\n", parse
                )
                self.assertLess(
                    parse.index('echo "DISPATCH_STATE_REPO secret is required"'),
                    parse.index("state_url="),
                )
                self.assertIn(
                    'state_url="https://api.github.com/repos/${DISPATCH_STATE_REPO}'
                    '/contents/${job_id}.json"',
                    parse,
                )
                # Every GitHub API repository URL must be parameterized, never a literal owner/name.
                self.assertEqual(
                    re.findall(r"api\.github\.com/repos/(?!\$\{)\S*", workflow), []
                )
                # The state repository name and URL are never echoed (the secret is masked anyway).
                for line in run_script(parse).splitlines():
                    if re.search(r"\becho\b|printf", line):
                        self.assertNotRegex(line, r"\$\{?(DISPATCH_STATE_REPO|state_url)\b")
                self.assertNotRegex(parse, r"(?m)^\s*set -[a-z]*x")

    def test_no_private_repository_literal_in_public_configuration(self):
        for path in GITHUB_DIR.rglob("*"):
            if not path.is_file() or "tests" in path.relative_to(GITHUB_DIR).parts:
                continue
            with self.subTest(path=path.name):
                self.assertEqual(re.findall(r"ttlgitproxy/[A-Za-z0-9_.-]+", read(path)), [])

    def test_manual_dispatch_input_does_not_describe_private_storage(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                self.assertIn(
                    "      job_id:\n"
                    "        description: 'Opaque dispatch job ID (UUID)'\n",
                    read(path),
                )


class DispatchPayloadTests(unittest.TestCase):
    def test_only_opaque_job_id_is_read_from_public_payload(self):
        for path in (BUILD, DEPLOY):
            workflow = read(path)
            with self.subTest(workflow=path.name):
                self.assertEqual(
                    set(re.findall(r"client_payload\.([A-Za-z_]+)", workflow)), {"job_id"}
                )
                self.assertNotIn("set_fallback_env", workflow)
                self.assertNotIn("fallback_project_id", workflow)

    def test_failure_status_tolerates_missing_project_id(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                report = steps(read(path))["Report failure status"]
                self.assertIn('if [ -z "${PROJECT_ID:-}" ] || [ -z "${SHA:-}" ]; then', report)
                self.assertNotIn('"$PROJECT_ID"', report)


class ParseDispatchPayloadBehaviourTests(unittest.TestCase):
    """Runs the real Parse step against a stub curl to prove private details stay out of output."""

    EVENT_TYPES = {BUILD: "build-and-test", DEPLOY: "deploy-testflight"}

    def setUp(self):
        self.bash = find_bash()
        if self.bash is None:
            self.skipTest("bash is not available")
        if shutil.which("jq") is None:
            self.skipTest("jq is not available")

    def state_body(self, path, **overrides):
        state = {
            "ref": "feature/synthetic",
            "sha": "a" * 40,
            "project_id": "42",
            "mr_iid": "7",
            "event_type": self.EVENT_TYPES[path],
        }
        state.update(overrides)
        content = base64.encodebytes(json.dumps(state).encode("utf-8")).decode("ascii")
        return json.dumps({"sha": "b" * 40, "content": content})

    def run_parse(self, path, repo=STATE_REPO, payload=None, get_status="200", get_rc=0,
                  get_body=None, probe_status=None, delete_status="200"):
        script = run_script(steps(read(path))["Parse dispatch payload"])
        self.assertNotIn("${{", script)
        self.assertIn("/usr/bin/base64 -D", script)
        script = script.replace("/usr/bin/base64 -D", "base64 -d")
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-parse-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        stub_bin, state, runner_temp = (base / name for name in ("bin", "state", "runner"))
        for folder in (stub_bin, state, runner_temp):
            folder.mkdir()
        write_executable(stub_bin / "curl", STUB_CURL)
        self.assertTrue(write_jq_shim(stub_bin))
        for name, value in (
            ("get-status", get_status), ("get-rc", get_rc), ("get-body", get_body),
            ("probe-status", probe_status), ("delete-status", delete_status),
        ):
            if value is not None:
                (state / name).write_bytes(str(value).encode("utf-8"))
        event = base / "event.json"
        event.write_text(
            json.dumps({"client_payload": payload if payload is not None else {"job_id": JOB_ID}}),
            encoding="utf-8",
        )
        github_env = runner_temp / "github_env"
        github_env.write_bytes(b"")
        (base / "step.sh").write_bytes((PATH_PRELUDE + script).encode("utf-8"))
        env = os.environ.copy()
        env.update(
            STUB_BIN=stub_bin.as_posix(),
            STUB_STATE=state.as_posix(),
            RUNNER_TEMP=runner_temp.as_posix(),
            GITHUB_ENV=github_env.as_posix(),
            GITHUB_EVENT_PATH=event.as_posix(),
            PROXY_PAT=PROXY_PAT,
            DISPATCH_STATE_REPO=repo,
            WORKFLOW_JOB_ID="",
            **MACOS_BASH,
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "step.sh"],
            cwd=base,
            env=env,
            capture_output=True,
        )
        calls_file = state / "calls.log"
        return {
            "status": result.returncode,
            "output": (result.stdout + result.stderr).decode("utf-8", errors="replace"),
            "calls": calls_file.read_text(encoding="utf-8").splitlines() if calls_file.exists() else [],
            "env": parse_env_file(github_env.read_text(encoding="utf-8")),
            "body_left": (runner_temp / "state-response.json").exists(),
        }

    def assert_private_details_withheld(self, outcome):
        for private in (STATE_REPO, "synthetic-state-store", PROXY_PAT, PRIVATE_BODY):
            self.assertNotIn(private, outcome["output"])
        self.assertFalse(outcome["body_left"])

    def test_state_is_fetched_from_secret_repository_and_exported(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                outcome = self.run_parse(
                    path, get_body=self.state_body(path, xcode_version="27")
                )
                self.assertEqual(outcome["status"], 0, outcome["output"])
                url = f"https://api.github.com/repos/{STATE_REPO}/contents/{JOB_ID}.json"
                self.assertEqual(outcome["calls"], [f"GET {url}", f"DELETE {url}"])
                self.assertEqual(
                    (
                        outcome["env"]["REF"],
                        outcome["env"]["SHA"],
                        outcome["env"]["PROJECT_ID"],
                        outcome["env"]["XCODE_VERSION"],
                    ),
                    ("feature/synthetic", "a" * 40, "42", "27"),
                )
                self.assert_private_details_withheld(outcome)

    def test_missing_or_malformed_secret_fails_before_any_request(self):
        cases = (
            ("", "DISPATCH_STATE_REPO secret is required"),
            ("synthetic-state-store", "must be in owner/name form"),
            (STATE_REPO + "/extra", "must be in owner/name form"),
            ("https://github.com/" + STATE_REPO, "must be in owner/name form"),
            (STATE_REPO + "\n", "must be in owner/name form"),
        )
        for path in (BUILD, DEPLOY):
            for repo, message in cases:
                with self.subTest(workflow=path.name, repo=repr(repo)):
                    outcome = self.run_parse(path, repo=repo, get_body=self.state_body(path))
                    self.assertEqual(outcome["status"], 1)
                    self.assertIn(message, outcome["output"])
                    self.assertEqual(outcome["calls"], [])
                    self.assertEqual(outcome["env"], {})
                    self.assert_private_details_withheld(outcome)

    def test_public_payload_fields_other_than_job_id_are_ignored(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                outcome = self.run_parse(
                    path,
                    payload={"job_id": "not-a-uuid", "project_id": "99", "sha": "c" * 40},
                    get_body=self.state_body(path),
                )
                self.assertEqual(outcome["status"], 1)
                self.assertIn("Invalid job_id format", outcome["output"])
                self.assertEqual(outcome["calls"], [])
                self.assertEqual(outcome["env"], {})

    def test_fetch_failures_are_diagnosed_without_printing_private_details(self):
        not_found = json.dumps({"message": "Not Found " + PRIVATE_BODY})
        cases = (
            ("401", 0, not_found, None, "HTTP 401"),
            ("403", 0, json.dumps({"message": "API rate limit exceeded " + PRIVATE_BODY}), None,
             "GitHub rate limit exceeded"),
            ("403", 0, json.dumps({"message": "Forbidden " + PRIVATE_BODY}), None,
             "may lack contents:read"),
            ("404", 0, not_found, "200", "never written or was already consumed"),
            ("404", 0, not_found, "401", "PROXY_PAT is invalid or expired"),
            ("404", 0, not_found, "404", "cause is not confirmed"),
            ("429", 0, not_found, None, "HTTP 429"),
            ("502", 0, not_found, None, "GitHub service failure"),
            ("000", 7, None, None, "transport/DNS failure"),
            ("200", 18, '{"sha": "' + PRIVATE_BODY, None, "transfer did not complete"),
            ("418", 0, not_found, None, "unexpected HTTP 418"),
            ("200", 0, "not json " + PRIVATE_BODY, None, "non-JSON or malformed"),
            ("200", 0, json.dumps({"sha": "b" * 40}), None, "missing .sha or .content"),
            ("200", 0, json.dumps({"sha": "b" * 40, "content": "!!" + PRIVATE_BODY}), None,
             "not valid base64"),
            ("200", 0, json.dumps({"sha": "b" * 40, "content": base64.b64encode(b"[1]").decode()}),
             None, "not a JSON object"),
        )
        for path in (BUILD, DEPLOY):
            for status, rc, body, probe, message in cases:
                with self.subTest(workflow=path.name, status=status, message=message):
                    outcome = self.run_parse(
                        path, get_status=status, get_rc=rc, get_body=body, probe_status=probe
                    )
                    self.assertEqual(outcome["status"], 1, outcome["output"])
                    self.assertIn(message, outcome["output"])
                    self.assertFalse(any(call.startswith("DELETE") for call in outcome["calls"]))
                    self.assertEqual(outcome["env"], {})
                    self.assert_private_details_withheld(outcome)

    def test_deploy_aborts_when_state_cannot_be_consumed(self):
        outcome = self.run_parse(DEPLOY, get_body=self.state_body(DEPLOY), delete_status="409")
        self.assertEqual(outcome["status"], 1)
        self.assertIn("aborting to prevent replay", outcome["output"])
        self.assertEqual(outcome["env"], {})
        self.assert_private_details_withheld(outcome)

    def test_multi_line_values_cannot_inject_environment_or_workflow_commands(self):
        injected = "\n__{0}_EOF__\nBASH_ENV=/tmp/synthetic\n::warning::SYNTHETIC-INJECTED\nX<<__{0}_EOF__"
        cases = (
            ("ref", "feature/synthetic" + injected.format("REF"), "Invalid ref format"),
            ("sha", "a" * 40 + injected.format("SHA"), "Invalid sha format"),
            ("project_id", "42" + injected.format("PROJECT_ID"), "Invalid project_id format"),
            # Embedded: MSYS bash strips a trailing CR in $(...), macOS bash does not.
            ("project_id", "4\r2", "Invalid project_id format"),
            ("xcode_version", "27" + injected.format("XCODE_VERSION"),
             "Invalid xcode_version format"),
            ("xcode_version", "27-beta", "Invalid xcode_version format"),
            ("xcode_version", "27.0.0", "Invalid xcode_version format"),
        )
        for path in (BUILD, DEPLOY):
            for field, value, message in cases:
                with self.subTest(workflow=path.name, field=field, value=repr(value[-12:])):
                    outcome = self.run_parse(path, get_body=self.state_body(path, **{field: value}))
                    self.assertEqual(outcome["status"], 1, outcome["output"])
                    self.assertIn(message, outcome["output"])
                    self.assertEqual(outcome["env"], {})
                    # Only the state file's blob SHA is masked before field validation.
                    masks = [line for line in outcome["output"].splitlines() if "::add-mask::" in line]
                    self.assertEqual(masks, ["::add-mask::" + "b" * 40])
                    self.assertNotIn("SYNTHETIC-INJECTED", outcome["output"])
                    self.assert_private_details_withheld(outcome)
        outcome = self.run_parse(
            BUILD, get_body=self.state_body(BUILD, mr_iid="7\n::warning::SYNTHETIC-INJECTED")
        )
        self.assertEqual(outcome["status"], 1, outcome["output"])
        self.assertIn("Invalid merge_request_iid format", outcome["output"])
        self.assertEqual(outcome["env"], {})
        self.assertNotIn("SYNTHETIC-INJECTED", outcome["output"])

    def test_invalid_state_blob_sha_is_rejected_before_masking(self):
        cases = (
            "B" * 40,
            "b" * 39,
            "b" * 40 + "\n::warning::SYNTHETIC-INJECTED",
            "b" * 20 + "\r" + "b" * 20,
        )
        for path in (BUILD, DEPLOY):
            for blob_sha in cases:
                with self.subTest(workflow=path.name, sha=repr(blob_sha[-12:])):
                    body = json.loads(self.state_body(path))
                    body["sha"] = blob_sha
                    outcome = self.run_parse(path, get_body=json.dumps(body))
                    self.assertEqual(outcome["status"], 1, outcome["output"])
                    self.assertIn("invalid .sha", outcome["output"])
                    self.assertNotIn("::add-mask::", outcome["output"])
                    self.assertNotIn("SYNTHETIC-INJECTED", outcome["output"])
                    self.assertFalse(any(call.startswith("DELETE") for call in outcome["calls"]))
                    self.assertEqual(outcome["env"], {})
                    self.assert_private_details_withheld(outcome)

    def test_multi_line_job_id_is_rejected_before_any_request(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                outcome = self.run_parse(
                    path,
                    payload={"job_id": JOB_ID + "\n::warning::SYNTHETIC-INJECTED"},
                    get_body=self.state_body(path),
                )
                self.assertEqual(outcome["status"], 1)
                self.assertIn("Invalid job_id format", outcome["output"])
                self.assertNotIn("SYNTHETIC-INJECTED", outcome["output"])
                self.assertEqual(outcome["calls"], [])

    def test_every_format_check_covers_the_whole_value(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                script = run_script(steps(read(path))["Parse dispatch payload"])
                self.assertIn("case \"$1\" in *$'\\n'*|*$'\\r'*) return 1 ;; esac", script)
                self.assertEqual(
                    re.findall(r"printf '%s' \"\$[A-Za-z_]\w*\" \| LC_ALL=C grep", script), []
                )
                expected = {"job_id", "file_sha", "ref", "sha", "project_id", "xcode_version"}
                if path == BUILD:
                    expected.add("merge_request_iid")
                self.assertEqual(set(re.findall(r'matches_whole "\$(\w+)"', script)), expected)


class XcodeRunnerRoutingTests(unittest.TestCase):
    def setUp(self):
        self.bash = find_bash()
        if self.bash is None:
            self.skipTest("bash is not available")
        if shutil.which("jq") is None:
            self.skipTest("jq is not available")

    def state_body(self, path, xcode_version=None):
        state = {
            "ref": "feature/synthetic",
            "sha": "a" * 40,
            "project_id": "42",
            "event_type": ParseDispatchPayloadBehaviourTests.EVENT_TYPES[path],
        }
        if xcode_version is not None:
            state["xcode_version"] = xcode_version
        content = base64.encodebytes(json.dumps(state).encode("utf-8")).decode("ascii")
        return json.dumps({"sha": "b" * 40, "content": content})

    def run_resolver(self, path, xcode_version=None):
        script = run_script(steps(read(path))["Resolve runner from job metadata"])
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-runner-resolution-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        stub_bin, state, runner_temp = (base / name for name in ("bin", "state", "runner"))
        for folder in (stub_bin, state, runner_temp):
            folder.mkdir()
        write_executable(stub_bin / "curl", STUB_CURL)
        self.assertTrue(write_jq_shim(stub_bin))
        (state / "get-body").write_text(
            self.state_body(path, xcode_version), encoding="utf-8"
        )
        event = base / "event.json"
        event.write_text(json.dumps({"client_payload": {"job_id": JOB_ID}}), encoding="utf-8")
        github_output = runner_temp / "github_output"
        github_output.write_bytes(b"")
        (base / "step.sh").write_bytes((PATH_PRELUDE + script).encode("utf-8"))
        env = os.environ.copy()
        env.update(
            STUB_BIN=stub_bin.as_posix(),
            STUB_STATE=state.as_posix(),
            RUNNER_TEMP=runner_temp.as_posix(),
            GITHUB_OUTPUT=github_output.as_posix(),
            GITHUB_EVENT_PATH=event.as_posix(),
            PROXY_PAT=PROXY_PAT,
            DISPATCH_STATE_REPO=STATE_REPO,
            WORKFLOW_JOB_ID="",
            **MACOS_BASH,
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "step.sh"],
            cwd=base,
            env=env,
            capture_output=True,
        )
        return (
            result.returncode,
            (result.stdout + result.stderr).decode("utf-8", errors="replace"),
            github_output.read_text(encoding="utf-8"),
        )

    def test_xcode_27_jobs_use_dedicated_runner(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, github_output = self.run_resolver(path, "27")
                self.assertEqual(status, 0, output)
                self.assertEqual(github_output, "runner=xcode-27\n")

    def test_jobs_without_requirement_keep_legacy_runner(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, github_output = self.run_resolver(path)
                self.assertEqual(status, 0, output)
                self.assertEqual(github_output, "runner=macos-latest\n")

    def test_unmapped_xcode_version_fails_before_runner_allocation(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, github_output = self.run_resolver(path, "28")
                self.assertEqual(status, 1)
                self.assertIn("No hosted runner mapping is configured for requested Xcode 28", output)
                self.assertEqual(github_output, "")

    def test_invalid_xcode_version_fails_before_runner_allocation(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, github_output = self.run_resolver(path, "27.0.0")
                self.assertEqual(status, 1)
                self.assertIn("Invalid xcode_version in dispatch state", output)
                self.assertEqual(github_output, "")


class XcodeSelectionTests(unittest.TestCase):
    def setUp(self):
        self.bash = find_bash()
        if self.bash is None:
            self.skipTest("bash is not available")

    def run_selection(self, path, required_version, selected_version, sdk_version,
                      installed_versions):
        script = run_script(steps(read(path))["Select Xcode"])
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-xcode-selection-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        applications = base / "Applications"
        stub_bin = base / "bin"
        applications.mkdir()
        stub_bin.mkdir()
        for version in installed_versions:
            (applications / f"Xcode_{version}.app").mkdir()
        write_executable(
            stub_bin / "sudo",
            '#!/usr/bin/env bash\nprintf "%s\\n" "$*" > "$STUB_STATE/selected"\n',
        )
        write_executable(
            stub_bin / "xcodebuild",
            f'#!/usr/bin/env bash\nprintf "Xcode {selected_version}\\nBuild version SYNTHETIC\\n"\n',
        )
        write_executable(
            stub_bin / "xcrun",
            f'#!/usr/bin/env bash\nprintf "{sdk_version}\\n"\n',
        )
        (base / "step.sh").write_bytes((PATH_PRELUDE + script).encode("utf-8"))
        env = os.environ.copy()
        env.update(
            STUB_BIN=stub_bin.as_posix(),
            STUB_STATE=base.as_posix(),
            XCODE_APPLICATIONS_DIR=applications.as_posix(),
            XCODE_VERSION=required_version,
            **MACOS_BASH,
        )
        result = subprocess.run(
            [self.bash, "--noprofile", "--norc", "step.sh"],
            cwd=base,
            env=env,
            capture_output=True,
        )
        selected = base / "selected"
        return (
            result.returncode,
            (result.stdout + result.stderr).decode("utf-8", errors="replace"),
            selected.read_text(encoding="utf-8") if selected.exists() else "",
            applications.as_posix(),
        )

    def test_required_xcode_and_sdk_are_selected_and_verified(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, selected, _ = self.run_selection(
                    path, "27", "27.0", "27.0", ("26.6", "27.0")
                )
                self.assertEqual(status, 0, output)
                self.assertIn("Xcode_27.0.app/Contents/Developer", selected)
                self.assertIn("Using iOS SDK 27.0", output)

    def test_missing_required_xcode_fails_without_exposing_paths(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, selected, applications = self.run_selection(
                    path, "27", "26.6", "26.5", ("26.6",)
                )
                self.assertEqual(status, 1)
                self.assertIn("Required Xcode 27 is unavailable", output)
                self.assertIn("Available Xcode versions: 26.6", output)
                self.assertNotIn(applications, output)
                self.assertEqual(selected, "")

    def test_wrong_sdk_generation_fails_early(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                status, output, _, _ = self.run_selection(
                    path, "27", "27.0", "26.5", ("27.0",)
                )
                self.assertEqual(status, 1)
                self.assertIn("requires the iOS 27 SDK generation", output)


class BuildWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.steps = steps(read(BUILD))

    def test_danger_report_is_not_published(self):
        danger = run_script(self.steps["Danger changeset rules (advisory)"])
        self.assertIn(
            'danger-swift local --base "origin/${DANGER_BASE_REF}" >/dev/null 2>&1', danger
        )

    def test_sanitizer_cannot_fail_the_build(self):
        step = self.steps["Sanitize log excerpt"]
        self.assertIn("        if: always()\n        continue-on-error: true\n", step)
        self.assertNotIn("working-directory", step.split("        run: |")[0])


class DeployLogSanitizerTests(unittest.TestCase):
    def setUp(self):
        self.workflow = read(DEPLOY)
        self.steps = steps(self.workflow)
        self.sanitize = self.steps["Sanitize deploy log for private status reporting"]
        self.excerpt = self.steps["Prepare sanitized deploy log excerpt"]

    def test_raw_log_regexes_run_in_c_locale(self):
        self.assertIn(ANSI_STRIP + ' deploy.log > "$temp_log"', self.sanitize)
        self.assertIn('LC_ALL=C sed -i.bak -E "s#${match}#${replace}#g" "$temp_log"', self.sanitize)
        self.assertEqual(re.findall(r"(?<!LC_ALL=C )sed -E \$'s/\\033", self.workflow), [])
        self.assertIn("""LC_ALL=C tr '`' "'\"""", self.excerpt)

    def test_sanitizer_failure_withholds_excerpt_without_failing_deploy(self):
        for step in (self.sanitize, self.excerpt):
            self.assertIn("\n        continue-on-error: true\n", step)
        self.assertLess(
            self.sanitize.index('rm -f "$temp_log" "$temp_log.bak" "$final_log"'),
            self.sanitize.index(ANSI_STRIP),
        )
        self.assertLess(
            self.sanitize.index(ANSI_STRIP), self.sanitize.index('mv "$temp_log" "$final_log"')
        )
        self.assertIn("steps.sanitize_deploy_log.outcome == 'success'", self.excerpt)

    def test_log_excerpt_is_only_prepared_for_failed_deploys(self):
        # factory-4 attaches log detail to failure comments only.
        self.assertIn("        if: failure() && hashFiles('source/deploy.log') != ''\n", self.sanitize)
        self.assertIn("        if: failure() && steps.sanitize_deploy_log.outcome", self.excerpt)
        comment = run_script(self.steps["Comment on source commit"])
        self.assertLess(comment.index("\nelse\n"), comment.index(EXCERPT_FILE))

    def test_no_deploy_log_is_published_as_an_artifact(self):
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                self.assertNotIn("upload-artifact", read(path))
        fastlane = self.steps["Build and upload with Fastlane"]
        self.assertNotIn("artifact", fastlane)

    def test_private_excerpt_is_passed_by_file(self):
        self.assertNotIn('>> "$GITHUB_ENV"', self.excerpt)
        self.assertNotIn("DEPLOY_ERROR_DETAIL", self.workflow)
        self.assertLess(
            self.excerpt.index('rm -f "$excerpt_file" "$excerpt_file.tmp"'),
            self.excerpt.index('printf \'%s\\n\' "$sanitized" > "$excerpt_file.tmp"'),
        )
        self.assertIn('mv "$excerpt_file.tmp" "$excerpt_file"', self.excerpt)
        comment = self.steps["Comment on source commit"]
        self.assertIn(EXCERPT_FILE, comment)
        self.assertIn('[ ! -L "$excerpt_file" ]', comment)
        cleanup = self.steps["Clean up"]
        self.assertIn('"$RUNNER_TEMP/deploy-log-excerpt.txt"', cleanup)

    def test_status_steps_do_not_echo_raw_curl_output(self):
        self.assertNotIn("invalid HTTP code '${http_code}'", self.workflow)

    def test_ansi_strip_handles_non_utf8_bytes(self):
        bash = find_bash()
        if bash is None:
            self.skipTest("bash is not available")
        command = next(
            line.strip() for line in self.sanitize.splitlines() if ANSI_STRIP in line
        )
        with tempfile.TemporaryDirectory(prefix="synthetic-sanitize-") as temporary:
            directory = Path(temporary)
            (directory / "deploy.log").write_bytes(
                b"\x1b[32m[12:00:00]: synthetic step\x1b[0m\n"
                b"\x1b[1;31mfailure \xff\xfe bytes\x1b[0m\n"
            )
            script = f'set -euo pipefail\ntemp_log="out.log"\n{command}\n'
            (directory / "step.sh").write_bytes(script.encode("utf-8"))
            subprocess.run(
                [bash, "--noprofile", "--norc", "step.sh"],
                cwd=directory,
                check=True,
                capture_output=True,
            )
            self.assertEqual(
                (directory / "out.log").read_bytes(),
                b"[12:00:00]: synthetic step\nfailure \xff\xfe bytes\n",
            )


class DeployFailureReportingBehaviourTests(unittest.TestCase):
    """Runs sanitize -> excerpt -> metadata on a synthetic failed deploy log."""

    SECRETS = {
        "ASC_KEY_ID": "SYNTHKEY12",
        "ASC_ISSUER_ID": "11111111-2222-3333-4444-555555555555",
        "SCHEME_NAME": "SyntheticScheme",
        "BUNDLE_ID": "com.example.synthetic",
        "TEAM_ID": "SYNTHTEAM1",
        "PROFILE_NAME": "Synthetic Profile Name",
    }
    PRIVATE = (
        b"/Users/", b"Synthetic Nobody", b"SYNTHTEAM1", b"Synthetic Profile Name",
        b"dev.person@example.invalid", b"hunter2", b"SYNTHKEY12", b"11111111-2222",
        b"com.example.synthetic", b"SyntheticScheme", b"__SANITIZED_EOF__", b"\x1b",
    )

    def setUp(self):
        self.bash = find_bash()
        if self.bash is None:
            self.skipTest("bash is not available")
        if shutil.which("jq") is None:
            self.skipTest("jq is not available")
        self.steps = steps(read(DEPLOY))

    def run_step(self, name, base, env):
        script = run_script(self.steps[name])
        self.assertNotIn("${{", script)
        step_file = base / (re.sub(r"\W+", "-", name) + ".sh")
        step_file.write_bytes((PATH_PRELUDE + script).encode("utf-8"))
        return subprocess.run(
            [self.bash, "--noprofile", "--norc", step_file.as_posix()],
            cwd=base / "source",
            env=env,
            capture_output=True,
        )

    def test_failed_deploy_log_is_redacted_and_bounded(self):
        filler = b"".join(b"[12:01:%02d]: compiling unit %d\n" % (n % 60, n) for n in range(120))
        log = (
            b"\x1b[32m[12:00:00]: fastlane 2.999.0\x1b[0m\n"
            b"[12:00:01]: $ cd /Users/runner/work/synthetic/source && agvtool new-version -all 42 && cd -\n"
            b"[12:00:02]: CODE_SIGN_IDENTITY=Apple Distribution: Synthetic Nobody (SYNTHTEAM1)\n"
            b"[12:00:03]: PROVISIONING_PROFILE_SPECIFIER=Synthetic Profile Name and more\n"
            b"[12:00:04]: Contact dev.person@example.invalid via https://user:hunter2@git.example.invalid/x.git\n"
            b"[12:00:05]: Using key SYNTHKEY12 issuer 11111111-2222-3333-4444-555555555555 \xff\xfe\n"
            + filler
            + b"[12:02:00]: error: Synthetic upload failure for SyntheticScheme (com.example.synthetic)\n"
            b"[12:02:01]: [!] Synthetic lane error __SANITIZED_EOF__ `quoted`\n"
        )
        with tempfile.TemporaryDirectory(prefix="synthetic-deploy-") as temporary:
            base = Path(temporary)
            source, runner_temp, stub_bin = (base / name for name in ("source", "runner", "bin"))
            for folder in (source, runner_temp, stub_bin):
                folder.mkdir()
            self.assertTrue(write_jq_shim(stub_bin))
            (source / "deploy.log").write_bytes(log)
            github_env = runner_temp / "github_env"
            github_env.write_bytes(b"")
            env = os.environ.copy()
            env.update(self.SECRETS)
            env.update(
                STUB_BIN=stub_bin.as_posix(),
                GITHUB_WORKSPACE=ROOT.as_posix(),
                RUNNER_TEMP=runner_temp.as_posix(),
                GITHUB_ENV=github_env.as_posix(),
                LANG="en_US.UTF-8",
                LC_ALL="en_US.UTF-8",
                **MACOS_BASH,
            )
            for name in (
                "Sanitize deploy log for private status reporting",
                "Prepare sanitized deploy log excerpt",
                "Extract deploy metadata",
            ):
                result = self.run_step(name, base, env)
                output = result.stdout + result.stderr
                with self.subTest(step=name):
                    self.assertEqual(result.returncode, 0, output.decode("utf-8", "replace"))
                    for private in self.PRIVATE:
                        self.assertNotIn(private, output)

            self.assertEqual(
                sorted(path.name for path in source.iterdir()),
                ["deploy-sanitized.log", "deploy.log"],
            )
            excerpt = (runner_temp / "deploy-log-excerpt.txt").read_bytes()
            self.assertFalse((runner_temp / "deploy-log-excerpt.txt.tmp").exists())
            for private in self.PRIVATE + (b"`",):
                self.assertNotIn(private, excerpt)
            self.assertIn(b"error: Synthetic upload failure for <scheme-name> (<bundle-id>)", excerpt)
            sanitized = (source / "deploy-sanitized.log").read_bytes()
            # The EOF guard is applied when the excerpt is cut; the full sanitized log is never published.
            for private in self.PRIVATE:
                if private != b"__SANITIZED_EOF__":
                    self.assertNotIn(private, sanitized)
            self.assertIn(b"<email>", sanitized)
            self.assertTrue(excerpt.endswith(b"--- [truncated; full log withheld for privacy] ---\n"))
            self.assertLessEqual(len(excerpt), 4096 + 1)
            self.assertLessEqual(len(excerpt.rstrip(b"\n").split(b"\n")), 60)

            exported = parse_env_file(github_env.read_text(encoding="utf-8"))
            self.assertEqual(exported["BUILD_NUMBER"], "42")
            self.assertEqual(exported["VERSION"], "unknown")
            self.assertIn("error: Synthetic upload failure", exported["DEPLOY_ERROR"])
            for private in self.PRIVATE:
                self.assertNotIn(private.decode("latin-1"), exported["DEPLOY_ERROR"])

    def test_excerpt_focus_ignores_routine_build_number_line(self):
        # "is build: 41" once matched an unanchored "ld: " linker-error pattern.
        lines = [b"[12:00:%02d]: compiling unit %d" % (n % 60, n) for n in range(120)]
        lines[3] = "[12:00:03]: \u26a0\ufe0f  ld: warning: ignoring duplicate libraries: '-lc++'".encode("utf-8")
        lines[5] = b"[12:00:05]: Latest upload for version 1.2 on ios platform is build: 41"
        lines[50] = b"[12:00:50]: Could not find synthetic upload target"
        lines.append(b"[!] Synthetic lane error")
        with tempfile.TemporaryDirectory(prefix="synthetic-excerpt-") as temporary:
            base = Path(temporary)
            source, runner_temp, stub_bin = (base / name for name in ("source", "runner", "bin"))
            for folder in (source, runner_temp, stub_bin):
                folder.mkdir()
            self.assertTrue(write_jq_shim(stub_bin))
            (source / "deploy-sanitized.log").write_bytes(b"\n".join(lines) + b"\n")
            env = os.environ.copy()
            env.update(
                STUB_BIN=stub_bin.as_posix(),
                GITHUB_WORKSPACE=ROOT.as_posix(),
                RUNNER_TEMP=runner_temp.as_posix(),
                **MACOS_BASH,
            )
            result = self.run_step("Prepare sanitized deploy log excerpt", base, env)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
            excerpt = (runner_temp / "deploy-log-excerpt.txt").read_bytes()
            self.assertIn(b"Could not find synthetic upload target", excerpt)
            self.assertIn(b"[!] Synthetic lane error", excerpt)
            self.assertNotIn(b"Latest upload for version", excerpt)
            self.assertNotIn(b"ld: warning:", excerpt)


class DeployMetadataTests(unittest.TestCase):
    def setUp(self):
        self.workflow = read(DEPLOY)
        self.step = steps(self.workflow)["Extract deploy metadata"]

    def test_public_release_metadata_is_not_registered_as_a_mask(self):
        self.assertNotIn('::add-mask::$build_number', self.step)
        self.assertNotIn('::add-mask::$version', self.step)

    def test_public_release_metadata_uses_a_private_file_not_github_env(self):
        self.assertNotIn("BUILD_NUMBER<<", self.step)
        self.assertNotIn("VERSION<<", self.step)
        self.assertIn(METADATA_FILE, self.step)
        self.assertIn('umask 077', self.step)
        self.assertIn('mktemp "$RUNNER_TEMP/deploy-metadata.XXXXXX"', self.step)
        self.assertIn('chmod 600 "$metadata_temp"', self.step)
        self.assertIn("jq -n \\", self.step)
        self.assertLess(self.step.index('mv "$metadata_temp" "$metadata_file"'),
                        self.step.index('echo "DEPLOY_ERROR<<'))

    def test_metadata_consumers_parse_and_revalidate_private_json(self):
        workflow_steps = steps(self.workflow)
        consumers = (
            workflow_steps["Report success status"],
            workflow_steps["Comment on source commit"],
        )
        for consumer in consumers:
            with self.subTest(step=consumer.splitlines()[0]):
                self.assertIn(METADATA_FILE, consumer)
                self.assertIn('[ -f "$metadata_file" ] && [ ! -L "$metadata_file" ]', consumer)
                self.assertIn("jq -er '.version | select(type == \"string\")'", consumer)
                self.assertIn("jq -er '.build_number | select(type == \"string\")'", consumer)
                self.assertIn("^[0-9]+(\\.[0-9]+){1,2}$", consumer)
                self.assertIn("''|*[!0-9]*)", consumer)
                self.assertNotRegex(consumer, r"\$\{?(VERSION|BUILD_NUMBER)\b")

    def test_cleanup_removes_metadata_and_partial_files(self):
        cleanup = steps(self.workflow)["Clean up"]
        self.assertIn('"$RUNNER_TEMP/deploy-metadata.json"', cleanup)
        self.assertIn('"$RUNNER_TEMP"/deploy-metadata.??????', cleanup)

    def test_private_identifiers_and_signing_metadata_remain_masked(self):
        for value in (
            "$SOURCE_REPOSITORY_URL",
            "${PROJECT_ID}",
            "${SHA:0:12}",
            "${REF}",
            "$keychain_password",
            "$profile_uuid",
            "$profile_name",
            "$profile_app_id",
            "$expiration_date",
            "$ASC_KEY_ID",
            "$ASC_ISSUER_ID",
        ):
            with self.subTest(value=value):
                self.assertIn(f'echo "::add-mask::{value}"', self.workflow)

    def test_loose_raw_log_patterns_are_gone(self):
        self.assertNotIn("Incrementing build number", self.step)
        self.assertNotIn("grep -oE '[0-9]+\\.[0-9]+", self.step)
        self.assertNotIn("DEPLOY_ERROR_DETAIL", self.step)
        self.assertIn(EXCERPT_FILE, self.step)
        self.assertIn("^[0-9]+(\\.[0-9]+){1,2}$", self.step)
        self.assertIn("'^[0-9]+$'", self.step)

    def run_metadata(self, deploy_log=None, plist=None, extra_plists=(), sanitized_log=None,
                     excerpt=None):
        bash = find_bash()
        if bash is None:
            self.skipTest("bash is not available")
        if shutil.which("jq") is None:
            self.skipTest("jq is not available")
        temporary = tempfile.TemporaryDirectory(prefix="synthetic-metadata-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        source = base / "source"
        runner_temp = base / "runner"
        stub_bin = base / "bin"
        source.mkdir()
        runner_temp.mkdir()
        stub_bin.mkdir()
        self.assertTrue(write_jq_shim(stub_bin))
        github_env = runner_temp / "github_env"
        github_env.write_bytes(b"")
        if deploy_log is not None:
            (source / "deploy.log").write_bytes(deploy_log)
        if sanitized_log is not None:
            (source / "deploy-sanitized.log").write_bytes(sanitized_log)
        if excerpt is not None:
            (runner_temp / "deploy-log-excerpt.txt").write_bytes(excerpt)
        if plist is not None:
            with zipfile.ZipFile(source / "Demo.ipa", "w") as archive:
                archive.writestr(
                    "Payload/Demo.app/Info.plist",
                    plistlib.dumps(plist, fmt=plistlib.FMT_BINARY),
                )
                for name, value in extra_plists:
                    archive.writestr(name, plistlib.dumps(value))
        # Strip CR so a Windows Python prints like /usr/bin/python3 on the macOS runner.
        python_wrapper = runner_temp / "python3.sh"
        python_wrapper.write_bytes(
            f'"{Path(sys.executable).as_posix()}" "$@" | tr -d \'\\r\'\n'.encode("utf-8")
        )
        script = run_script(self.step).replace(
            "/usr/bin/python3", "bash " + shlex.quote(python_wrapper.as_posix())
        )
        (runner_temp / "step.sh").write_bytes((PATH_PRELUDE + script).encode("utf-8"))
        environment = dict(os.environ)
        # Simulate the runner's UTF-8 locale; the step must not depend on it.
        environment.update(
            {
                "STUB_BIN": stub_bin.as_posix(),
                "GITHUB_ENV": github_env.as_posix(),
                "RUNNER_TEMP": runner_temp.as_posix(),
                "LANG": "en_US.UTF-8",
                "LC_ALL": "en_US.UTF-8",
                **MACOS_BASH,
            }
        )
        # surrogateescape keeps non-UTF-8 log bytes round-trippable for byte-exact assertions.
        result = subprocess.run(
            [bash, "--noprofile", "--norc", "-eo", "pipefail", "../runner/step.sh"],
            cwd=source,
            env=environment,
            capture_output=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
        self.assertEqual(result.returncode, 0, ascii(result.stderr))
        self.assertFalse((runner_temp / "read-ipa-version.py").exists())
        env_text = github_env.read_bytes().decode("utf-8", errors="surrogateescape")
        metadata = json.loads((runner_temp / "deploy-metadata.json").read_text(encoding="utf-8"))
        return result.stdout, parse_env_file(env_text), metadata

    def assert_only_masked(self, stdout, *values):
        for value in values:
            self.assertIn(f"::add-mask::{value}", stdout.splitlines())
            for line in stdout.splitlines():
                if value in line:
                    self.assertTrue(line.startswith("::add-mask::"), line)

    def test_metadata_comes_from_exported_ipa(self):
        stdout, env, metadata = self.run_metadata(
            deploy_log=(
                b"[12:00:00]: fastlane 2.999.0\n"
                b"[12:00:01]: $ cd /synthetic && agvtool new-version -all 111 && cd -\n"
            ),
            plist={"CFBundleShortVersionString": "3.4.5", "CFBundleVersion": "678"},
            extra_plists=(
                (
                    "Payload/Demo.app/Frameworks/Kit.framework/Info.plist",
                    {"CFBundleShortVersionString": "9.9.9", "CFBundleVersion": "999"},
                ),
            ),
        )
        self.assertNotIn("VERSION", env)
        self.assertNotIn("BUILD_NUMBER", env)
        self.assertEqual((metadata["version"], metadata["build_number"]), ("3.4.5", "678"))
        self.assertNotIn("::add-mask::3.4.5", stdout)
        self.assertNotIn("::add-mask::678", stdout)

    def test_build_number_falls_back_to_anchored_agvtool_line(self):
        stdout, env, metadata = self.run_metadata(
            deploy_log=(
                b"[12:00:00]: fastlane 2.999.0\n"
                b"[12:00:01]: Latest upload for version 2.0.1 is build: 41\n"
                b"[12:00:02]: \x1b[36m$ cd /synthetic\\ dir && agvtool new-version -all 42 && cd -\x1b[0m\n"
                b"[12:00:03]: build_number 777 \xff\xfe\n"
            )
        )
        self.assertNotIn("VERSION", env)
        self.assertNotIn("BUILD_NUMBER", env)
        self.assertEqual((metadata["version"], metadata["build_number"]), ("unknown", "42"))
        self.assertNotIn("::add-mask::42", stdout)
        self.assertNotIn("::add-mask::unknown", stdout)

    def test_invalid_ipa_values_are_rejected(self):
        stdout, env, metadata = self.run_metadata(
            deploy_log=b"[12:00:00]: fastlane 2.999.0\n",
            plist={"CFBundleShortVersionString": "1.2.3.4", "CFBundleVersion": "12a"},
        )
        self.assertNotIn("VERSION", env)
        self.assertNotIn("BUILD_NUMBER", env)
        self.assertEqual(
            (metadata["version"], metadata["build_number"]), ("unknown", "unknown")
        )
        self.assertNotIn("::add-mask::", stdout)

    def test_deploy_error_with_non_utf8_bytes_is_cut_without_failing(self):
        long_line = b"[12:00:00]: error: synthetic failure \xff\xfe " + b"x" * 300
        excerpt = (
            b"synthetic   context \xff\n" + long_line
            + b"\n--- [truncated; full log withheld for privacy] ---\n"
        )
        cases = (
            ("sanitized log", {"sanitized_log": b"[12:00:00]: step\n" + long_line + b"\n"},
             long_line),
            ("excerpt file", {"excerpt": excerpt}, re.sub(rb"\s+", b" ", excerpt)),
        )
        for name, files, expected in cases:
            with self.subTest(source=name):
                stdout, env, metadata = self.run_metadata(**files)
                # Cut by character; invalid bytes become U+FFFD so GitLab accepts the text.
                self.assertEqual(env["DEPLOY_ERROR"], expected.decode("utf-8", "replace")[:200])
                env["DEPLOY_ERROR"].encode("utf-8")
                self.assertNotIn("VERSION", env)
                self.assertNotIn("BUILD_NUMBER", env)
                self.assertEqual(
                    (metadata["version"], metadata["build_number"]), ("unknown", "unknown")
                )
                self.assert_only_masked(stdout, env["DEPLOY_ERROR"])

    def test_cut_never_splits_a_multibyte_character(self):
        line = "[!] " + "\u00e9" * 300
        stdout, env, _ = self.run_metadata(sanitized_log=line.encode("utf-8") + b"\n")
        self.assertEqual(env["DEPLOY_ERROR"], line[:200])

    def test_headline_prefers_fastlane_summary_and_skips_build_number_line(self):
        routine = (
            b"[12:00:00]: Latest upload for version 1.2 on ios platform is build: 41\n"
            b"[!] Synthetic earlier tool warning\n"
            b"[12:00:01]: $ cd /synthetic && agvtool new-version -all 42 && cd -\n"
            b"[12:00:02]: ld: warning: ignoring duplicate libraries: '-lc++'\n"
            b"[12:01:00]: Upload failed: synthetic rejection\n"
            b"[12:01:01]: error: synthetic compile failure\n"
        )
        cases = (
            (routine + b"[!] Synthetic lane error\n", "[!] Synthetic lane error"),
            (routine.replace(b"[!] Synthetic earlier tool warning\n", b""),
             "[12:01:01]: error: synthetic compile failure"),
        )
        for log, expected in cases:
            with self.subTest(expected=expected):
                stdout, env, _ = self.run_metadata(sanitized_log=log)
                self.assertEqual(env["DEPLOY_ERROR"], expected)
                self.assert_only_masked(stdout, expected)

    def test_percent_in_error_is_escaped_for_the_mask_under_bash_3_2(self):
        stdout, env, _ = self.run_metadata(
            sanitized_log=b"[!] Synthetic 100% failure %0A\n"
        )
        self.assertEqual(env["DEPLOY_ERROR"], "[!] Synthetic 100% failure %0A")
        masks = [line for line in stdout.splitlines() if line.startswith("::add-mask::")]
        self.assertEqual(masks, ["::add-mask::[!] Synthetic 100%25 failure %250A"])


class CutLocaleTests(unittest.TestCase):
    def test_every_cut_runs_in_c_locale(self):
        # BSD cut -c rejects invalid UTF-8 under the runner's UTF-8 locale, and the
        # byte-wise sanitizers can pass such bytes through.
        for path in (BUILD, DEPLOY):
            with self.subTest(workflow=path.name):
                cuts = [
                    (line, match)
                    for line in read(path).splitlines()
                    for match in re.finditer(r"(?<![\w-])cut\s+-", line)
                ]
                self.assertTrue(any("cut -c" in line for line, _ in cuts))
                for line, match in cuts:
                    self.assertTrue(
                        line[:match.start()].endswith("LC_ALL=C "),
                        f"cut runs without LC_ALL=C: {line.strip()}",
                    )
        metadata = steps(read(DEPLOY))["Extract deploy metadata"]
        self.assertNotIn("cut -c1-200", metadata)
        self.assertEqual(metadata.count("| jq -Rrs '.[0:200]')\""), 2)


class RunScriptExpressionTests(unittest.TestCase):
    # Only runner-controlled values may be expanded into script text; step outputs and
    # event data must be passed through env: so they are never parsed as shell code.
    ALLOWED = {
        "${{ github.server_url }}",
        "${{ github.repository }}",
        "${{ github.run_id }}",
        "${{ job.status }}",
    }

    def test_run_scripts_only_expand_runner_controlled_expressions(self):
        for path in (BUILD, DEPLOY):
            workflow = read(path)
            self.assertEqual(re.findall(r"^\s*run:[ \t]+(?!\|$).+$", workflow, re.M), [])
            for name, step in steps(workflow).items():
                if "        run: |\n" not in step:
                    continue
                with self.subTest(workflow=path.name, step=name):
                    found = set(re.findall(r"\$\{\{[^}]*\}\}", run_script(step)))
                    self.assertLessEqual(found, self.ALLOWED)


if __name__ == "__main__":
    unittest.main()
