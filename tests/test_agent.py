import http.client
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from wechat_ai_assistant.agent import AgentConfig, AgentStore, LocalAgent
from wechat_ai_assistant.Errors import NoSuchFriendError
from wechat_ai_assistant.cloud import CloudClient, CloudError
from wechat_ai_assistant.cloud_server import _load_agent_tokens, create_server
from wechat_ai_assistant.reliability import MessageEvent


class AgentTests(unittest.TestCase):
    def test_local_agent_serializes_dialog_operations(self):
        active = 0
        maximum = 0
        guard = threading.Lock()

        def opener(**kwargs):
            nonlocal active, maximum
            with guard:
                active += 1
                maximum = max(maximum, active)
            time.sleep(0.03)
            with guard:
                active -= 1
            return object()

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"))
            first = LocalAgent(config, store=store, dialog_opener=opener)
            second = LocalAgent(config, store=store, dialog_opener=opener)
            threads = [threading.Thread(target=agent._open_dialog) for agent in (first, second)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertEqual(maximum, 1)
            first.close()
            second.close()
            store.close()

    def test_local_agent_retries_dialog_lookup(self):
        calls = []

        class Dialog:
            def close(self):
                pass

        def opener(**kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise NoSuchFriendError
            return Dialog()

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"))
            agent = LocalAgent(config, store=store, dialog_opener=opener)
            self.assertIsInstance(agent._open_dialog(), Dialog)
            self.assertEqual(len(calls), 3)
            agent.close()
            store.close()

    def test_local_agent_survives_heartbeat_failure(self):
        class Cloud:
            def send_events(self, events):
                return None

            def fetch_commands(self):
                return []

            def acknowledge(self, command_id, result):
                return None

            def heartbeat(self, health):
                raise OSError("heartbeat offline")

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="0s")
            agent = LocalAgent(config, store=store, cloud=Cloud())
            summary = agent.run_once()
            self.assertEqual(summary["health"]["state"], "degraded")
            agent.close()
            store.close()

    def test_history_bootstrap_keeps_identical_rows_and_runs_once(self):
        calls = []
        item = {"消息发送人": "a", "消息内容": "收到", "消息类型": "文本", "消息发送时间": "12:30"}
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=2, listen_duration="0s")
            agent = LocalAgent(
                config,
                store=store,
                history_reader=lambda **kwargs: calls.append(kwargs) or [item, item],
            )
            first = agent.run_once()
            second = agent.run_once()
            self.assertEqual(first["history_events"], 2)
            self.assertEqual(second["history_events"], 0)
            self.assertEqual(len(calls), 1)
            self.assertEqual(store.event_count(), 2)
            agent.close()
            store.close()

    def test_history_event_preserves_media_kind_for_review_queue(self):
        with tempfile.TemporaryDirectory() as directory:
            store=AgentStore(Path(directory)/"agent.db")
            agent=LocalAgent(AgentConfig(group="chat",history_limit=0,listen_duration="0s"),store=store)
            self.assertEqual(agent._history_event({"消息内容":"[图片]","消息类型":"图片"}).kind.value,"image")
            self.assertEqual(agent._history_event({"消息内容":"[语音]","消息类型":"语音"}).kind.value,"voice")
            agent.close();store.close()

    def test_business_event_outbox_retries_independently_and_survives_restart(self):
        class Cloud:
            def __init__(self):self.business_calls=0;self.event_calls=0
            def send_events(self,events):self.event_calls+=1
            def send_business_events(self,events):
                self.business_calls+=1
                if self.business_calls==1:raise OSError("temporary business API outage")
            def fetch_commands(self):return []
            def heartbeat(self,health):return None
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/"agent.db";store=AgentStore(path);cloud=Cloud()
            config=AgentConfig(group="chat",db_path=str(path),history_limit=0,listen_duration="0s")
            agent=LocalAgent(config,store=store,cloud=cloud);event=MessageEvent("chat","hello",stable_id="stable-1")
            agent._record_event(event);agent._publish([event])
            self.assertEqual(store.pending_business_upload_count(),1)
            with store._lock,store._connection:store._connection.execute("UPDATE business_uploads SET next_attempt_at=0")
            store.close()
            resumed=AgentStore(path);agent.store=resumed;agent._publish([])
            self.assertEqual(cloud.event_calls,1)
            self.assertEqual(cloud.business_calls,2)
            self.assertEqual(resumed.pending_business_upload_count(),0)
            agent.close();resumed.close()

    def test_local_agent_persists_live_sequence(self):
        class Dialog:
            def close(self):
                pass

        calls = []

        def listener(**kwargs):
            calls.append(kwargs["sequence_start"])
            if len(calls) == 1:
                event = MessageEvent("chat", "live", sequence=kwargs["sequence_start"])
                return {"消息事件": [event.to_dict()]}
            return {"消息事件": []}

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="1s")
            agent = LocalAgent(config, store=store, dialog_opener=lambda **kwargs: Dialog(), listener=listener)
            agent.run_once()
            agent.run_once()
            self.assertEqual(calls, [0, 1])
            self.assertEqual(store.get_meta("message_sequence"), "1")
            self.assertEqual(store.recent_events(1)[0]["identity_quality"], "sequence")
            agent.close()
            store.close()

    def test_local_agent_deduplicates_live_runtime_snapshots(self):
        class Dialog:
            def close(self):
                pass

        def listener(**kwargs):
            event = MessageEvent("chat", "live", runtime_id=(1, 2))
            return {"消息事件": [event.to_dict()]}

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db")
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="1s")
            agent = LocalAgent(config, store=store, dialog_opener=lambda **kwargs: Dialog(), listener=listener)
            first = agent.run_once()
            second = agent.run_once()
            self.assertEqual(first["live_events"], 1)
            self.assertEqual(second["live_events"], 0)
            self.assertEqual(store.event_count(), 1)
            stored = store.recent_events(1)[0]
            self.assertIsNone(stored["stable_id"])
            self.assertEqual(stored["identity_quality"], "runtime")
            agent.close()
            store.close()

    def test_local_agent_retries_event_upload(self):
        now = [100.0]

        class Cloud:
            def __init__(self):
                self.calls = 0

            def send_events(self, events):
                self.calls += 1
                if self.calls == 1:
                    raise OSError("offline")

            def fetch_commands(self):
                return []

            def acknowledge(self, command_id, result):
                return None

            def heartbeat(self, health):
                return None

        class Dialog:
            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: now[0])
            cloud = Cloud()
            config = AgentConfig(group="chat", db_path=str(Path(directory) / "agent.db"), history_limit=0, listen_duration="1s")
            event = MessageEvent("chat", "live", runtime_id=(1,))
            agent = LocalAgent(
                config,
                store=store,
                dialog_opener=lambda **kwargs: Dialog(),
                listener=lambda **kwargs: {"消息事件": [event.to_dict()]},
                cloud=cloud,
            )
            agent.run_once()
            self.assertEqual(store.pending_upload_count(), 1)
            now[0] = 106.0
            agent.run_once()
            self.assertEqual(store.pending_upload_count(), 0)
            self.assertEqual(cloud.calls, 2)
            agent.close()
            store.close()

    def test_event_upload_claim_is_exclusive(self):
        now = [100.0]
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: now[0])
            store.record_event(MessageEvent("chat", "hello", stable_id="m-upload"))
            first = store.claim_event_uploads(lease_seconds=10)
            self.assertEqual(len(first), 1)
            self.assertEqual(store.claim_event_uploads(lease_seconds=10), [])
            self.assertFalse(store.mark_event_uploaded(first[0][0], "wrong-token"))
            self.assertTrue(store.mark_event_upload_failed(first[0][0], "offline", retry_delay=1, lease_token=first[0][2]))
            now[0] = 102.0
            second = store.claim_event_uploads(lease_seconds=10)
            self.assertEqual(len(second), 1)
            self.assertTrue(store.mark_event_uploaded(second[0][0], lease_token=second[0][2]))
            store.close()

    def test_event_upload_stops_after_max_attempts(self):
        with tempfile.TemporaryDirectory() as directory:
            store = AgentStore(Path(directory) / "agent.db", clock=lambda: 100.0)
            store.record_event(MessageEvent("chat", "poison", stable_id="m-poison"))
            for _ in range(10):
                claimed = store.claim_event_uploads(lease_seconds=10)
                self.assertEqual(len(claimed), 1)
                self.assertTrue(store.mark_event_upload_failed(claimed[0][0], "permanent", retry_delay=0, lease_token=claimed[0][2]))
            self.assertEqual(store.pending_upload_count(), 0)
            self.assertEqual(store.failed_upload_count(), 1)
            self.assertEqual(store.claim_event_uploads(), [])
            store.close()

    def test_console_page_and_state_require_console_token(self):
        with tempfile.TemporaryDirectory() as directory:
            server, store = create_server("127.0.0.1", 0, "secret", str(Path(directory) / "cloud.db"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
            try:
                connection.request("GET", "/console")
                response = connection.getresponse()
                body = response.read()
                self.assertEqual(response.status, 200)
                self.assertIn(b"wechat_ai_assistant", body)
                connection.request("GET", "/api/v1/console/state")
                self.assertEqual(connection.getresponse().status, 401)
                connection.request("GET", "/api/v1/console/state", headers={"Authorization": "Bearer secret"})
                response = connection.getresponse()
                state = json.loads(response.read())
                self.assertEqual(response.status, 200)
                self.assertIn("health", state)
            finally:
                connection.close()
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=2)

    def test_agent_token_env_rejects_non_string_values(self):
        with patch.dict(os.environ, {"WECHAT_AI_AGENT_TOKENS": '{"agent-1": null}'}):
            with self.assertRaises(ValueError):
                _load_agent_tokens()

    def test_reference_server_binds_agent_token(self):
        with tempfile.TemporaryDirectory() as directory:
            server, store = create_server(
                "127.0.0.1",
                0,
                "",
                str(Path(directory) / "cloud.db"),
                agent_tokens={"agent-1": "secret-1"},
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_address[1]}"
                CloudClient(base_url, "secret-1", agent_id="agent-1").heartbeat({"state": "idle"})
                with self.assertRaises(CloudError):
                    CloudClient(base_url, "secret-1", agent_id="agent-2").heartbeat({"state": "idle"})
            finally:
                server.shutdown()
                server.server_close()
                store.close()
                thread.join(timeout=2)

    def test_reference_server_requires_per_agent_tokens_off_loopback(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                create_server("0.0.0.0", 0, "shared-token", str(Path(directory) / "cloud.db"))

    def test_cloud_client_requires_https_for_remote_endpoint(self):
        with self.assertRaises(ValueError):
            CloudClient("http://example.com", "token")
        client = CloudClient("http://127.0.0.1:8000", "token")
        self.assertEqual(client.base_url, "http://127.0.0.1:8000")


if __name__ == "__main__":
    unittest.main()
