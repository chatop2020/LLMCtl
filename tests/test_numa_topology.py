#!/usr/bin/env python3
"""验证 PLE Worker 的 NUMA 自动绑定与安全降级契约。"""

import pathlib
import shlex
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
TOPOLOGY = ROOT / "lib/llmctl/topology.sh"
MANAGER = ROOT / "llmctl.sh"


class NumaTopologyTests(unittest.TestCase):
    """用隔离 sysfs 和固定拓扑矩阵验证不同硬件路径。"""

    def run_binding(
        self, matrix: str, devices: str, cpu_lists: dict[int, str]
    ) -> subprocess.CompletedProcess[str]:
        """运行一次 NUMA 解析，不访问测试机真实 GPU 或 sysfs。"""

        with tempfile.TemporaryDirectory() as directory:
            node_root = pathlib.Path(directory)
            for node, cpus in cpu_lists.items():
                target = node_root / f"node{node}"
                target.mkdir()
                (target / "cpulist").write_text(cpus + "\n", encoding="utf-8")
            script = f"""
                set -Eeuo pipefail
                warn() {{ printf 'WARN: %s\n' "$*" >&2; }}
                export LLM_SYS_NODE_ROOT={shlex.quote(str(node_root))}
                export TEST_TOPOLOGY={shlex.quote(matrix)}
                nvidia-smi() {{ printf '%s\n' "$TEST_TOPOLOGY"; }}
                source {shlex.quote(str(TOPOLOGY))}
                worker_numa_binding {shlex.quote(devices)}
            """
            return subprocess.run(
                ["bash", "-c", script], check=False, text=True, capture_output=True
            )

    def test_same_node_gpu_group_returns_local_cpus(self):
        """同一 NUMA 的 TP2 组必须返回节点及完整本地 CPU 列表。"""

        result = self.run_binding(
            "GPU0 X NODE 0-47,96-143 0 N/A\nGPU1 NODE X 0-47,96-143 0 N/A",
            "0,1",
            {0: "0-47,96-143"},
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "0\t0-47,96-143")

    def test_dual_socket_eight_gpu_matrix_maps_both_halves_locally(self):
        """现场双路八卡矩阵必须把前后四卡分别映射到 NUMA 0 和 1。"""

        matrix = "\n".join(
            [
                "GPU0 X NODE NODE NODE SYS SYS SYS SYS 0-47,96-143 0 N/A",
                "GPU1 NODE X NODE NODE SYS SYS SYS SYS 0-47,96-143 0 N/A",
                "GPU2 NODE NODE X NODE SYS SYS SYS SYS 0-47,96-143 0 N/A",
                "GPU3 NODE NODE NODE X SYS SYS SYS SYS 0-47,96-143 0 N/A",
                "GPU4 SYS SYS SYS SYS X NODE NODE NODE 48-95,144-191 1 N/A",
                "GPU5 SYS SYS SYS SYS NODE X NODE NODE 48-95,144-191 1 N/A",
                "GPU6 SYS SYS SYS SYS NODE NODE X NODE 48-95,144-191 1 N/A",
                "GPU7 SYS SYS SYS SYS NODE NODE NODE X 48-95,144-191 1 N/A",
            ]
        )

        first = self.run_binding(
            matrix, "2,3", {0: "0-47,96-143", 1: "48-95,144-191"}
        )
        second = self.run_binding(
            matrix, "6,7", {0: "0-47,96-143", 1: "48-95,144-191"}
        )

        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stdout.strip(), "0\t0-47,96-143")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stdout.strip(), "1\t48-95,144-191")

    def test_cross_node_gpu_group_falls_back_without_binding(self):
        """跨 NUMA GPU 组不得被强制限制到任意单一内存节点。"""

        result = self.run_binding(
            "GPU0 X SYS 0-47 0 N/A\nGPU4 SYS X 48-95 1 N/A",
            "0,4",
            {0: "0-47", 1: "48-95"},
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("跨 NUMA 0/1", result.stderr)

    def test_missing_numa_affinity_falls_back_without_binding(self):
        """驱动未提供 NUMA Affinity 时应保持 Docker 默认调度。"""

        result = self.run_binding("GPU0 X 0-31 N/A N/A", "0", {})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("没有可用 NUMA Affinity", result.stderr)

    def test_worker_applies_binding_only_before_ple_image_launch(self):
        """NUMA 参数必须只在 PLE 路径中作为 Docker 镜像前参数加入。"""

        source = MANAGER.read_text(encoding="utf-8")
        worker = source.split("cmd_worker_start() {", 1)[1].split(
            "image_supports_architecture() {", 1
        )[0]
        binding = worker.index('numa_binding=$(worker_numa_binding "${GPU_DEVICES}")')
        image = worker.index('"${VLLM_IMAGE}" /model')

        self.assertIn("if (( PLE_CPU_OFFLOAD == 1 ))", worker)
        self.assertIn('--cpuset-mems "${numa_node}"', worker)
        self.assertIn('--cpuset-cpus "${numa_cpus}"', worker)
        self.assertLess(binding, image)


if __name__ == "__main__":
    unittest.main()
