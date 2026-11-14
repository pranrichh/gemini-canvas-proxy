import base64
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "gemini_proxy",
    ROOT / "native_host" / "gemini_proxy.py",
)
proxy = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(proxy)


class OpenAIToGeminiTests(unittest.TestCase):
    def test_system_instruction_and_role_mapping(self):
        result = proxy.openai_to_gemini({
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Question"},
                {"role": "assistant", "content": "Answer"},
            ]
        })

        self.assertEqual(
            result["systemInstruction"],
            {"parts": [{"text": "Be concise."}]},
        )
        self.assertEqual(
            result["contents"],
            [
                {"role": "user", "parts": [{"text": "Question"}]},
                {"role": "model", "parts": [{"text": "Answer"}]},
            ],
        )

    def test_multimodal_data_uri(self):
        result = proxy.openai_to_gemini({
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Inspect"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "data:image/png;base64,aGVsbG8="},
                    },
                ],
            }]
        })

        self.assertEqual(
            result["contents"][0]["parts"],
            [
                {"text": "Inspect"},
                {
                    "inlineData": {
                        "mimeType": "image/png",
                        "data": "aGVsbG8=",
                    }
                },
            ],
        )

    def test_pdf_video_and_audio_ride_the_data_uri_path(self):
        cases = [
            ("data:application/pdf;base64,JVBERi0=", "application/pdf"),
            ("data:video/mp4;base64,AAAAIGZ0eXA=", "video/mp4"),
            ("data:audio/wav;base64,UklGRg==", "audio/wav"),
        ]
        for uri, expected in cases:
            with self.subTest(mime=expected):
                result = proxy.openai_to_gemini({
                    "messages": [{"role": "user", "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": uri}},
                    ]}]
                })
                inline = result["contents"][0]["parts"][1]["inlineData"]
                self.assertEqual(inline["mimeType"], expected)

    def test_openai_input_audio_part(self):
        result = proxy.openai_to_gemini({
            "messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": "UklGRg==", "format": "wav"}},
            ]}]
        })
        inline = result["contents"][0]["parts"][0]["inlineData"]
        self.assertEqual(inline["mimeType"], "audio/wav")
        self.assertEqual(inline["data"], "UklGRg==")

    def test_file_part_with_base64_data(self):
        result = proxy.openai_to_gemini({
            "messages": [{"role": "user", "content": [
                {"type": "file", "file": {
                    "filename": "paper.pdf",
                    "file_data": "data:application/pdf;base64,JVBERi0=",
                }},
            ]}]
        })
        inline = result["contents"][0]["parts"][0]["inlineData"]
        self.assertEqual(inline["mimeType"], "application/pdf")

    def test_unsupported_media_type_is_refused(self):
        with self.assertRaises(proxy.RequestError):
            proxy.openai_to_gemini({
                "messages": [{"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": "data:application/zip;base64,AAAA"}},
                ]}]
            })

    def test_assistant_tool_calls_become_function_calls(self):
        result = proxy.openai_to_gemini({
            "messages": [{
                "role": "assistant",
                "content": "Using tools",
                "tool_calls": [{
                    "id": "call_one",
                    "type": "function",
                    "x_gemini_thought_signature": "signature-one",
                    "function": {
                        "name": "lookup",
                        "arguments": '{"query":"value"}',
                    },
                }],
            }]
        })

        self.assertEqual(
            result["contents"][0]["parts"][1],
            {
                "functionCall": {
                    "name": "lookup",
                    "args": {"query": "value"},
                },
                "thoughtSignature": "signature-one",
            },
        )

    def test_consecutive_tool_messages_share_one_user_turn(self):
        result = proxy.openai_to_gemini({
            "messages": [
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call_a",
                            "x_gemini_thought_signature": "sig-a",
                            "function": {"name": "alpha", "arguments": "{}"},
                        },
                        {
                            "id": "call_b",
                            "x_gemini_thought_signature": "sig-b",
                            "function": {"name": "beta", "arguments": "{}"},
                        },
                    ],
                },
                {"role": "tool", "tool_call_id": "call_a", "content": '{"a":1}'},
                {"role": "tool", "tool_call_id": "call_b", "content": '{"b":2}'},
            ]
        })

        self.assertEqual(len(result["contents"]), 2)
        tool_turn = result["contents"][1]
        self.assertEqual(tool_turn["role"], "user")
        self.assertEqual(
            [part["functionResponse"]["name"] for part in tool_turn["parts"]],
            ["alpha", "beta"],
        )

    def test_schema_sanitization_and_recursive_type_uppercasing(self):
        result = proxy.openai_to_gemini({
            "messages": [],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "nested",
                    "parameters": {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "title": "drop",
                                    "properties": {
                                        "value": {
                                            "type": "string",
                                            "format": "uri",
                                        }
                                    },
                                },
                            }
                        },
                    },
                },
            }],
        })

        schema = result["tools"][0]["functionDeclarations"][0]["parameters"]
        serialized = json.dumps(schema)
        for forbidden in ("$schema", "additionalProperties", "title", "format"):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(schema["type"], "OBJECT")
        self.assertEqual(
            schema["properties"]["items"]["items"]["properties"]["value"]["type"],
            "STRING",
        )

    def test_generation_parameters(self):
        result = proxy.openai_to_gemini({
            "messages": [],
            "temperature": 0.2,
            "max_completion_tokens": 120,
            "top_p": 0.9,
            "top_k": 40,
            "stop": ["END", "STOP"],
            "seed": 7,
            "frequency_penalty": 0.3,
            "presence_penalty": 0.4,
            "response_format": {"type": "json_object"},
        })

        self.assertEqual(
            result["generationConfig"],
            {
                "temperature": 0.2,
                "maxOutputTokens": 120,
                "topP": 0.9,
                "topK": 40,
                "stopSequences": ["END", "STOP"],
                "seed": 7,
                "frequencyPenalty": 0.3,
                "presencePenalty": 0.4,
                "responseMimeType": "application/json",
            },
        )

    def test_scalar_stop_sequence(self):
        result = proxy.openai_to_gemini({"messages": [], "stop": "DONE"})
        self.assertEqual(result["generationConfig"]["stopSequences"], ["DONE"])


class GeminiToOpenAITests(unittest.TestCase):
    def test_text_response(self):
        result = proxy.gemini_to_openai({
            "candidates": [{
                "content": {"parts": [{"text": "hello"}]},
                "finishReason": "STOP",
            }]
        }, "gemini-test")

        choice = result["choices"][0]
        self.assertEqual(choice["message"]["content"], "hello")
        self.assertEqual(choice["finish_reason"], "stop")

    def test_inline_image_response(self):
        result = proxy.gemini_to_openai({
            "candidates": [{
                "content": {
                    "parts": [{
                        "inlineData": {
                            "mimeType": "image/png",
                            "data": "aGVsbG8=",
                        }
                    }]
                },
                "finishReason": "STOP",
            }]
        }, "gemini-test")

        self.assertEqual(
            result["choices"][0]["message"]["content"],
            "![generated_image](data:image/png;base64,aGVsbG8=)",
        )

    def test_single_and_parallel_function_calls_preserve_signatures(self):
        result = proxy.gemini_to_openai({
            "candidates": [{
                "content": {
                    "parts": [
                        {
                            "functionCall": {"name": "alpha", "args": {"a": 1}},
                            "thoughtSignature": "sig-a",
                        },
                        {
                            "functionCall": {"name": "beta", "args": {"b": 2}},
                            "thoughtSignature": "sig-b",
                        },
                    ]
                },
                "finishReason": "STOP",
            }]
        }, "gemini-test")

        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        calls = choice["message"]["tool_calls"]
        self.assertEqual([call["function"]["name"] for call in calls], ["alpha", "beta"])
        self.assertEqual(
            [call["x_gemini_thought_signature"] for call in calls],
            ["sig-a", "sig-b"],
        )
        self.assertTrue(all(call["id"].startswith("call_") for call in calls))

    def test_all_finish_reasons_map_to_openai_values(self):
        expected = {
            "STOP": "stop",
            "MAX_TOKENS": "length",
            "SAFETY": "content_filter",
            "RECITATION": "content_filter",
            "BLOCKLIST": "content_filter",
            "PROHIBITED_CONTENT": "content_filter",
            "SPII": "content_filter",
            "IMAGE_SAFETY": "content_filter",
            "LANGUAGE": "content_filter",
            "OTHER": "stop",
            "MALFORMED_FUNCTION_CALL": "stop",
            "UNEXPECTED_TOOL_CALL": "stop",
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            for source, target in expected.items():
                with self.subTest(source=source):
                    result = proxy.gemini_to_openai({
                        "candidates": [{
                            "content": {"parts": [{"text": "value"}]},
                            "finishReason": source,
                        }]
                    }, "gemini-test")
                    self.assertEqual(result["choices"][0]["finish_reason"], target)
        # Every documented reason is mapped, so none of them should warn.
        self.assertEqual(stderr.getvalue(), "")

    def test_unknown_finish_reason_warns_and_falls_back_to_stop(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            result = proxy.gemini_to_openai({
                "candidates": [{
                    "content": {"parts": [{"text": "value"}]},
                    "finishReason": "SOMETHING_NEW",
                }]
            }, "gemini-test")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertIn("SOMETHING_NEW", stderr.getvalue())

    def test_empty_candidates_raise_with_safety_details(self):
        with self.assertRaises(proxy.GeminiResponseError) as raised:
            proxy.gemini_to_openai({
                "promptFeedback": {
                    "blockReason": "SAFETY",
                    "safetyRatings": [{"category": "TEST", "blocked": True}],
                }
            }, "gemini-test")

        self.assertEqual(raised.exception.details["blockReason"], "SAFETY")
        self.assertTrue(raised.exception.details["safetyRatings"])

    def test_empty_non_stop_candidate_raises(self):
        with self.assertRaises(proxy.GeminiResponseError):
            proxy.gemini_to_openai({
                "candidates": [{
                    "content": {"parts": []},
                    "finishReason": "SAFETY",
                }]
            }, "gemini-test")


class RoundTripTests(unittest.TestCase):
    def test_tool_call_ids_link_results_and_signatures_round_trip(self):
        first_response = proxy.gemini_to_openai({
            "candidates": [{
                "content": {
                    "parts": [{
                        "functionCall": {
                            "name": "get_weather",
                            "args": {"city": "Paris"},
                        },
                        "thoughtSignature": "real-signature",
                    }]
                },
                "finishReason": "STOP",
            }]
        }, "gemini-test")
        assistant = first_response["choices"][0]["message"]
        call = assistant["tool_calls"][0]

        translated = proxy.openai_to_gemini({
            "messages": [
                {"role": "user", "content": "Weather?"},
                assistant,
                {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": '{"temperature":20}',
                },
            ]
        })

        function_call = translated["contents"][1]["parts"][0]
        function_response = translated["contents"][2]["parts"][0]
        self.assertEqual(function_call["thoughtSignature"], "real-signature")
        self.assertEqual(
            function_response["functionResponse"]["name"],
            "get_weather",
        )
        self.assertEqual(
            function_response["functionResponse"]["response"],
            {"temperature": 20},
        )


class ChunkingTests(unittest.TestCase):
    def test_large_non_ascii_payload_round_trips_byte_identically(self):
        payload = {
            "type": "api_request",
            "id": "unicode",
            "body": {"text": "你好🌍 café — " * 250_000},
        }
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

        chunks = proxy.chunk_request(serialized, payload["id"])
        reassembled = b"".join(
            base64.b64decode(chunk["chunk_data"])
            for chunk in chunks
        )

        self.assertGreater(len(chunks), 1)
        self.assertEqual(reassembled, serialized)
        for chunk in chunks:
            encoded = json.dumps(chunk, separators=(",", ":")).encode("utf-8")
            self.assertLessEqual(len(encoded), proxy.MAX_HOST_TO_BROWSER_BYTES)


if __name__ == "__main__":
    unittest.main()
