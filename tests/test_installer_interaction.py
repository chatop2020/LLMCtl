#!/usr/bin/env python3
import pathlib
import subprocess
import tempfile
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
    def test_modelscope_downloader_path_is_not_captured_with_install_logs(self):
        installer = INSTALLER.read_text(encoding="utf-8")
        download = installer.split("ensure_modelscope_downloader() {", 1)[1].split(
            "validate_downloaded_model() {", 1
        )[0]
        model_download = installer.split("download_model() {", 1)[1].split(
            "make_active_workers() {", 1
        )[0]
        self.assertIn(
            'MODELSCOPE_DOWNLOADER="${venv}/bin/ms"', download
        )
        self.assertNotIn("printf '%s\\n'", download)
        self.assertIn("ensure_modelscope_downloader", model_download)
        self.assertIn('"${MODELSCOPE_DOWNLOADER}" download', model_download)
        self.assertNotIn("$(ensure_modelscope_downloader)", model_download)
        self.assertNotIn("ms-hub", download)
        self.assertIn('download --help >/dev/null', download)
        self.assertIn('importlib.metadata.version("modelscope-hub") == "0.1.8"', download)
        self.assertIn("--force-reinstall", download)

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
                    catalog_result_count() { printf '1\n'; }
                    show_catalog_selection_summary() { printf 'summary <%s> <%s>\n' "$1" "$2"; }
                    apply_catalog_selection() {
                      printf 'select <%s> <%s>\n' "$1" "$2"
                    }
                    select_model_interactively
                    """
                ),
            ],
            input="2\nornith\n\n1\ny\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("search <modelscope> <ornith> <auto>", completed.stdout)
        self.assertIn("select </tmp/test-catalog.json> <1>", completed.stdout)

    def test_model_confirmation_can_return_to_candidate_list(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    search_catalog() { CATALOG_RESULTS=/tmp/test-catalog.json; }
                    catalog_result_count() { printf '2\n'; }
                    show_catalog_selection_summary() { printf 'summary <%s>\n' "$2"; }
                    apply_catalog_selection() { printf 'selected <%s>\n' "$2"; }
                    select_model_interactively
                    """
                ),
            ],
            input="2\nornith\n\n1\nb\n2\ny\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("summary <1>", completed.stdout)
        self.assertIn("summary <2>", completed.stdout)
        self.assertIn("selected <2>", completed.stdout)

    def test_direct_or_validated_plan_can_return_before_installation(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    MODEL_HUB=huggingface
                    MODEL_ID=example/Test
                    MODEL_REVISION=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
                    run_catalog_with_retry() { printf '{}\n'; }
                    show_catalog_selection_summary() { printf 'detailed-plan\n'; }
                    if plan_single_model_interactively; then
                      printf 'unexpected-accept\n'
                    else
                      printf 'returned-before-install\n'
                    fi
                    """
                ),
            ],
            input="b\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("detailed-plan", completed.stdout)
        self.assertIn("returned-before-install", completed.stdout)
        self.assertNotIn("unexpected-accept", completed.stdout)

    def test_model_selection_can_return_to_discovery(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    search_catalog() { CATALOG_RESULTS=/tmp/test-catalog.json; }
                    catalog_result_count() { printf '1\n'; }
                    discard_catalog_results() { CATALOG_RESULTS=''; }
                    plan_single_model_interactively() { return 0; }
                    select_model_interactively
                    printf 'source=%s\n' "$MODEL_SOURCE"
                    """
                ),
            ],
            input="2\nornith\n\n0\n4\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("source=validated", completed.stdout)

    def test_english_language_selects_english_model_menu(self):
        completed = subprocess.run(
            [
                "bash",
                "-c",
                installer_script(
                    r"""
                    select_language_interactively
                    plan_single_model_interactively() { return 0; }
                    select_model_interactively
                    printf 'language=%s source=%s\n' "$INTERFACE_LANGUAGE" "$MODEL_SOURCE"
                    """
                ),
            ],
            input="2\n4\n",
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Choose how to discover a model", completed.stderr)
        self.assertIn("language=en source=validated", completed.stdout)

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

    def test_english_help_works_even_when_lang_follows_help(self):
        completed = subprocess.run(
            ["bash", str(INSTALLER), "--help", "--lang", "en"],
            text=True,
            capture_output=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Universal vLLM cluster installer", completed.stdout)
        self.assertNotIn("通用 vLLM 集群自动安装器", completed.stdout)

    def test_exact_local_model_is_reusable_but_mismatched_identity_is_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            revision = "a" * 40
            target = root / "legacy-ornith-weights"
            target.mkdir()
            (target / "config.json").write_text(
                '{"architectures":["Qwen3ForCausalLM"]}', encoding="utf-8"
            )
            (target / "model.safetensors").write_bytes(b"weight")
            (root / "current.manifest").write_text(
                "\n".join(
                    (
                        "MODEL_ID=example/Test",
                        f"MODEL_REVISION={revision}",
                        "MODEL_HUB=huggingface",
                    )
                )
                + "\n",
                encoding="utf-8",
            )
            (root / "current").symlink_to(target.name)
            completed = subprocess.run(
                [
                    "bash",
                    "-c",
                    installer_script(
                        f"""
                        MODEL_ROOT={root!s}
                        MODEL_ID=example/Test
                        MODEL_REVISION={revision}
                        MODEL_HUB=huggingface
                        MODEL_ARCHITECTURE=Qwen3ForCausalLM
                        MODEL_WEIGHT_BYTES=1
                        reusable_model_at_root "$MODEL_ROOT" && printf 'exact=yes\n'
                        MODEL_ID=example/Other
                        if reusable_model_at_root "$MODEL_ROOT"; then printf 'mismatch=yes\n'; else printf 'mismatch=no\n'; fi
                        """
                    ),
                ],
                text=True,
                capture_output=True,
            )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("exact=yes", completed.stdout)
        self.assertIn("mismatch=no", completed.stdout)


if __name__ == "__main__":
    unittest.main()
