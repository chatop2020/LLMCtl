#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import tempfile
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MANAGER = ROOT / "llmctl.sh"


class KeepWarmBehaviorTests(unittest.TestCase):
    def test_workers_are_probed_directly_with_a_bounded_non_stream_request(self):
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            fake_bin = tmp / "bin"
            fake_bin.mkdir()
            curl_log = tmp / "curl.log"
            fake_curl = fake_bin / "curl"
            fake_curl.write_text(
                textwrap.dedent(
                    """\
                    #!/usr/bin/env bash
                    set -euo pipefail
                    output="" data="" url="" auth=""
                    while (($#)); do
                      case "$1" in
                        -o) output="$2"; shift 2 ;;
                        --data-binary) data="$2"; shift 2 ;;
                        -H)
                          [[ "$2" != Authorization:* ]] || auth="$2"
                          shift 2
                          ;;
                        http://*) url="$1"; shift ;;
                        *) shift ;;
                      esac
                    done
                    printf '{"choices":[{"message":{"content":"OK"}}]}' >"${output}"
                    printf '%s\t%s\t%s\n' "${url}" "${auth}" "${data}" >>"${FAKE_CURL_LOG}"
                    printf '200\t0.012\t0.025'
                    """
                ),
                encoding="utf-8",
            )
            fake_curl.chmod(0o755)
            state_dir = tmp / "state"
            script = textwrap.dedent(
                f"""\
                set -Eeuo pipefail
                export LLMCTL_SOURCE_ONLY=1
                export LLM_CLUSTER_STATE_DIR={state_dir!s}
                source {MANAGER!s}
                INTERFACE_LANGUAGE=en
                WORKER_BASE_PORT=8100
                TP_SIZE=1
                SERVED_MODEL_NAME=test-model
                SUPPORTS_THINKING_TOGGLE=1
                BACKEND_API_KEY=sk-backend-test
                KEEPWARM_TIMEOUT_SECONDS=5
                install -d -m 0700 "${{KEEPWARM_STATE_DIR}}"
                keepwarm_one_worker 0 "${{KEEPWARM_STATE_DIR}}/0.json" &
                keepwarm_one_worker 1 "${{KEEPWARM_STATE_DIR}}/1.json" &
                wait
                """
            )
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            env["FAKE_CURL_LOG"] = str(curl_log)
            process = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

            results = [
                json.loads((state_dir / "keepwarm" / f"{worker}.json").read_text(encoding="utf-8"))
                for worker in (0, 1)
            ]
            self.assertEqual({item["worker"] for item in results}, {0, 1})
            self.assertTrue(all(item["status"] == "ok" for item in results))

            calls = [line.split("\t", 2) for line in curl_log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(
                {call[0] for call in calls},
                {
                    "http://127.0.0.1:8100/v1/chat/completions",
                    "http://127.0.0.1:8101/v1/chat/completions",
                },
            )
            self.assertTrue(all(call[1] == "Authorization: Bearer sk-backend-test" for call in calls))
            for _, _, payload in calls:
                request = json.loads(payload)
                self.assertEqual(request["model"], "test-model")
                self.assertIs(request["stream"], False)
                self.assertEqual(request["max_tokens"], 1)
                self.assertIs(request["chat_template_kwargs"]["enable_thinking"], False)


if __name__ == "__main__":
    unittest.main()
