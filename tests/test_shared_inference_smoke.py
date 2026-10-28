import io
import json
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts import shared_inference_smoke as smoke


def envelope(content, finish_reason="stop"):
    return {"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]}


class ResponseValidationTests(unittest.TestCase):
    def test_both_workloads_accept_their_own_strict_schema(self):
        smoke.validate_response(envelope('{"language":"da","reply":"Fred."}'), "history")
        smoke.validate_response(envelope('{"speaker":"examiner","reply":"Hello."}'), "medical")

    def test_rejects_truncation_even_if_json_is_valid(self):
        with self.assertRaises(smoke.InvalidResponse):
            smoke.validate_response(envelope('{"language":"da","reply":"Fred."}', "length"), "history")

    def test_rejects_invalid_envelopes_and_invalid_content(self):
        invalid = [
            {}, {"choices": []}, {"choices": [None]},
            envelope("not json"), envelope("[]"),
            envelope('{"language":"da","reply":"  "}'),
            envelope('{"language":"da","reply":7}'),
            envelope('{"language":"en","reply":"Peace"}'),
            envelope('{"language":"da","reply":"Fred","extra":true}'),
            envelope('{"language":"da"}'),
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(smoke.InvalidResponse):
                smoke.validate_response(value, "history")

    def test_medical_schema_rejects_wrong_speaker_and_extra_fields(self):
        for content in (
            '{"speaker":"patient","reply":"Hello"}',
            '{"speaker":"examiner","reply":"Hello","diagnosis":"invented"}',
            '{"speaker":"examiner","reply":""}',
        ):
            with self.subTest(content=content), self.assertRaises(smoke.InvalidResponse):
                smoke.validate_response(envelope(content), "medical")


class RequestTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.server.request_path = self.path
                self.server.request_body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                self.send_response(self.server.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(self.server.response).encode())

            def log_message(self, *args):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.server.status = 200
        self.server.response = envelope('{"language":"da","reply":"Fred."}')

    def test_http_request_and_optional_thinking_flag(self):
        result = smoke.run_case(self.url, "test-model", "history", 2, disable_thinking=True)
        self.assertTrue(result["ok"])
        self.assertEqual(self.server.request_path, "/v1/chat/completions")
        self.assertEqual(self.server.request_body["model"], "test-model")
        self.assertEqual(self.server.request_body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertGreaterEqual(result["elapsed_seconds"], 0)

    def test_thinking_flag_is_not_sent_by_default(self):
        self.assertTrue(smoke.run_case(self.url, "test-model", "history", 2)["ok"])
        self.assertNotIn("chat_template_kwargs", self.server.request_body)

    def test_http_errors_and_timeout_are_failures(self):
        self.server.status = 503
        result = smoke.run_case(self.url, "test-model", "history", 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "HTTP 503")
        with patch.object(smoke.request, "urlopen", side_effect=TimeoutError("private detail")):
            result = smoke.run_case(self.url, "test-model", "history", 2)
        self.assertFalse(result["ok"])
        self.assertNotIn("private detail", json.dumps(result))

    def test_timeout_bounds_a_stalled_name_lookup(self):
        release = threading.Event()

        def stalled(*args, **kwargs):
            release.wait(1)
            raise TimeoutError()

        try:
            with patch.object(smoke.request, "urlopen", side_effect=stalled):
                result = smoke.run_case(self.url, "test-model", "history", 0.05)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "timeout")
            self.assertLess(result["elapsed_seconds"], 0.5)
        finally:
            release.set()

    def test_invalid_json_response_is_a_failure(self):
        self.server.response = envelope("not JSON")
        result = smoke.run_case(self.url, "test-model", "history", 2)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid response")

    def test_cli_returns_nonzero_without_printing_response_bodies(self):
        self.server.status = 503
        self.server.response = {"error": "private server detail"}
        output = io.StringIO()
        with redirect_stdout(output):
            code = smoke.main(["--base-url", self.url, "--model", "test-model"])
        self.assertEqual(code, 1)
        self.assertNotIn("private server detail", output.getvalue())
        self.assertEqual(len(json.loads(output.getvalue())["results"]), 2)


if __name__ == "__main__":
    unittest.main()
