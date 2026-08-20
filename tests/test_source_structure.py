#!/usr/bin/env python3
"""防止已经拆分的控制面重新退化为超大单文件。"""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
MAX_SOURCE_LINES = 3000


class SourceStructureTests(unittest.TestCase):
    """检查本次拆分涉及的后端、前端和 CLI 权威源码。"""

    def test_control_plane_sources_remain_below_the_file_limit(self):
        """每个受管第一方源码文件都必须保持在 3000 个物理行以内。"""

        files = [
            *sorted((ROOT / "lib").glob("account_portal*.py")),
            ROOT / "lib/model_deployment.py",
            ROOT / "lib/model_upgrade.py",
            ROOT / "llmctl.sh",
            *sorted((ROOT / "lib/llmctl").glob("*.sh")),
            ROOT / "portal-ui/src/App.vue",
            ROOT / "portal-ui/src/useModelDeployments.js",
            ROOT / "portal-ui/src/portalWorkspaceContext.js",
            *sorted((ROOT / "portal-ui/src/components").glob("*.vue")),
        ]
        oversized = {
            str(path.relative_to(ROOT)): len(
                path.read_text(encoding="utf-8").splitlines()
            )
            for path in files
            if len(path.read_text(encoding="utf-8").splitlines()) > MAX_SOURCE_LINES
        }
        self.assertEqual(oversized, {})

    def test_split_runtime_modules_are_present_in_upgrade_manifest(self):
        """结构拆分后的模块必须随首次安装和控制面升级一起交付。"""

        manifest = (ROOT / "upgrade-manifest.tsv").read_text(encoding="utf-8")
        for path in [
            *sorted((ROOT / "lib").glob("account_portal_*.py")),
            ROOT / "lib/model_upgrade.py",
        ]:
            self.assertIn(str(path.relative_to(ROOT)), manifest)
        self.assertIn("dir     lib/llmctl", manifest)


if __name__ == "__main__":
    unittest.main()
