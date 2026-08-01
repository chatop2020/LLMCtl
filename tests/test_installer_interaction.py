#!/usr/bin/env python3
import pathlib
import subprocess
import textwrap
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install-llm-cluster.sh"


def installer_script(body: str) -> str:
    return textwrap.dedent(
        f"""
        export INSTALLER_SOURCE_ONLY=1
        source {INSTALLER!s}
        {body}
        """
    )


class InstallerInteractionTests(unittest.TestCase):
    def test_interactive_modelscope_search_accepts_default_auto_task(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    search_catalog() {
                      printf 'search <%s> <%s> <%s>\n' "$1" "$CATALOG_QUERY" "$CATALOG_TASK"
                      CATALOG_RESULTS=/tmp/test-catalog.json
                    }
                    apply_catalog_selection() {
                      printf 'select <%s> <%s>\n' "$1" "$2"
                    }
                    select_model_interactively
                    """
                ),
            ],
            input="2\nornith\n\n1\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("search <modelscope> <ornith> <auto>", completed.stdout)
        self.assertIn("select </tmp/test-catalog.json> <1>", completed.stdout)

    def test_catalog_search_with_default_context_returns_successfully(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    mktemp() { printf '/tmp/test-catalog.json\n'; }
                    run_catalog_with_retry() {
                      printf 'catalog-command'
                      printf ' <%s>' "$@"
                      printf '\n'
                    }
                    CATALOG_QUERY=ornith
                    CATALOG_TASK=auto
                    MAX_LEN_EXPLICIT=0
                    search_catalog modelscope
                    printf 'search-complete\n'
                    """
                ),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("search-complete", completed.stdout)
        self.assertIn("<--gpu-memory-utilization> <0.92>", completed.stdout)
        self.assertNotIn("<--max-model-len>", completed.stdout)

    def test_catalog_search_includes_explicit_context(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    mktemp() { printf '/tmp/test-catalog.json\n'; }
                    run_catalog_with_retry() {
                      printf 'catalog-command'
                      printf ' <%s>' "$@"
                      printf '\n'
                    }
                    CATALOG_QUERY=ornith
                    CATALOG_TASK=vision
                    MAX_LEN_EXPLICIT=1
                    MAX_MODEL_LEN=32768
                    search_catalog modelscope
                    """
                ),
            ],
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("<--max-model-len> <32768>", completed.stdout)

    def test_unexpected_errexit_is_visible(self):
        completed = subprocess.run(
            ["bash", "-c", installer_script("false")],
            text=True,
            capture_output=True,
        )
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("安装器在第", completed.stderr)
        self.assertIn("意外失败", completed.stderr)


if __name__ == "__main__":
    unittest.main()
