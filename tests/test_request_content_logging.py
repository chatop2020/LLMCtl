import pathlib
import sys
import types
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
LIB = str(ROOT / "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)

import account_portal_common as common
import account_portal_gateway as gateway


class RequestContentLoggingTests(unittest.TestCase):
    def client(self):
        """创建不访问真实网络的 OmniRoute 管理客户端。"""

        config = types.SimpleNamespace(
            gateway_manage_key="management-key",
            gateway_url="http://127.0.0.1:9",
        )
        return gateway.OmniRouteClient(config)

    def test_enables_pipeline_without_changing_other_log_settings(self):
        client = self.client()
        current = {
            "logs": {
                "detailedLogsEnabled": False,
                "callLogPipelineEnabled": False,
                "maxDetailSizeKb": 10,
                "ringBufferSize": 500,
            }
        }
        updated = {
            "logs": {**current["logs"], "callLogPipelineEnabled": True}
        }
        client.request = mock.Mock(side_effect=[current, updated])

        self.assertTrue(client.ensure_request_content_logging())
        self.assertEqual(
            client.request.call_args_list,
            [
                mock.call("GET", "/api/settings/database"),
                mock.call("PATCH", "/api/settings/database", updated),
            ],
        )

    def test_already_enabled_pipeline_is_idempotent(self):
        client = self.client()
        client.request = mock.Mock(
            return_value={
                "logs": {
                    "detailedLogsEnabled": False,
                    "callLogPipelineEnabled": True,
                    "maxDetailSizeKb": 10,
                    "ringBufferSize": 500,
                }
            }
        )

        self.assertFalse(client.ensure_request_content_logging())
        client.request.assert_called_once_with("GET", "/api/settings/database")

    def test_portal_returns_retained_content_up_to_one_million_characters(self):
        long_text = "长" * 120_000
        request = common.request_content_summary(
            {"messages": [{"role": "user", "content": long_text}]}
        )
        response = common.response_content_summary(
            {"choices": [{"message": {"role": "assistant", "content": long_text}}]}
        )

        self.assertFalse(request["truncated"])
        self.assertFalse(response["truncated"])
        self.assertEqual(request["messages"][0]["content"], long_text)
        self.assertEqual(response["messages"][0]["content"], long_text)


if __name__ == "__main__":
    unittest.main()
