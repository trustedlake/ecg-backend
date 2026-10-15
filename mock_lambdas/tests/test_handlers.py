"""Testes do contrato v1 das lambdas mock (sem AWS; S3 falso em memória).

    pip install pillow boto3
    python -m unittest discover -s mock_lambdas/tests -v
"""

import importlib.util
import io
import json
import os
import sys
import unittest
import uuid

from botocore.exceptions import ClientError
from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("RESULTS_BUCKET", "results-test")
os.environ.pop("MOCK_SCENARIO", None)

from ecg_contract import ERRORS, LAYOUTS, WARNINGS  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(f"{name}_handler", os.path.join(ROOT, name, "handler.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reading = _load("reading")
interpretation = _load("interpretation")


class FakeS3:
    def __init__(self):
        self.objects = {}

    def head_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.objects[(Bucket, Key)] = Body


def _image_bytes(fmt="JPEG", size=(1600, 1000)):
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 235, 235)).save(buf, format=fmt)
    return buf.getvalue()


class ReadingTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()
        reading.s3 = self.s3
        self.s3.objects[("images", "foto.jpg")] = _image_bytes()
        self.request_id = str(uuid.uuid4())

    def call(self, **overrides):
        event = {"contract_version": "1.0", "request_id": self.request_id,
                 "image": {"bucket": "images", "key": "foto.jpg"}, "layout": "3x4+1",
                 "calibration": {"speed_mm_s": 25, "gain_mm_mv": 10}}
        event.update(overrides)
        return reading.handler(event, None)

    def assertError(self, response, code, status="ERROR"):
        self.assertEqual(response["status"], status, response["error"])
        self.assertEqual(response["error"]["code"], code)
        self.assertEqual(response["error"]["category"], ERRORS[code][0])
        self.assertTrue(response["error"]["message"])
        self.assertIsNone(response["result"])

    def test_success_returns_signal_overlay_and_grid(self):
        response = self.call()
        self.assertEqual(response["status"], "OK")
        self.assertEqual(response["warnings"], [])
        self.assertEqual(response["request_id"], self.request_id)
        result = response["result"]
        self.assertEqual(len(result["leads"]), 13)  # 12 + tira de ritmo II
        rhythm = [lead for lead in result["leads"] if lead["kind"] == "rhythm"]
        self.assertEqual([(lead["name"], len(lead["samples"])) for lead in rhythm], [("II", 5000)])
        self.assertTrue(all(len(lead["samples"]) == 1250 for lead in result["leads"] if lead["kind"] == "panel"))
        self.assertGreater(result["grid"]["small_box_px"], 0)
        overlay = result["artifacts"]["overlay"]
        png = Image.open(io.BytesIO(self.s3.objects[(overlay["bucket"], overlay["key"])]))
        self.assertEqual((png.format, png.size), ("PNG", (1600, 1000)))
        self.assertTrue(overlay["key"].startswith(f"reading/{self.request_id}/"))
        stored = json.loads(self.s3.objects[("results-test", result["artifacts"]["signal"]["key"])])
        self.assertEqual(stored["leads"][0]["samples"], result["leads"][0]["samples"])
        self.assertNotIn("_x_start", result["leads"][0])

    def test_all_layouts(self):
        for layout, (rows, rhythm) in LAYOUTS.items():
            with self.subTest(layout=layout):
                response = self.call(layout=layout)
                self.assertEqual(response["status"], "OK")
                self.assertEqual(len(response["result"]["leads"]), sum(map(len, rows)) + len(rhythm))

    def test_success_with_warnings(self):
        response = self.call(mock={"scenario": "success_with_warnings"})
        self.assertEqual(response["status"], "OK_WITH_WARNINGS")
        codes = {w["code"] for w in response["warnings"]}
        self.assertEqual(codes, {"LEAD_PARTIAL_TRACE", "LEAD_SATURATED", "IMAGE_ROTATION_CORRECTED"})
        self.assertTrue(codes <= set(WARNINGS))
        v2 = next(lead for lead in response["result"]["leads"] if lead["name"] == "V2")
        self.assertIn(None, v2["samples"])
        self.assertEqual(v2["quality"], "partial")

    def test_failure_default_and_specific_code(self):
        self.assertError(self.call(mock={"scenario": "failure"}), "READ_TRACE_EXTRACTION_FAILED")
        self.assertError(self.call(mock={"scenario": "failure", "code": "READ_GRID_NOT_FOUND"}), "READ_GRID_NOT_FOUND")

    def test_refusal_with_reason_and_preview(self):
        response = self.call(mock={"scenario": "refused", "code": "READ_REFUSED_OCCLUDED"})
        self.assertError(response, "READ_REFUSED_OCCLUDED", status="REFUSED")
        preview = response["error"]["details"]["preview"]
        self.assertIn((preview["bucket"], preview["key"]), self.s3.objects)
        self.assertError(self.call(mock={"scenario": "refused"}), "READ_REFUSED_BLURRY", status="REFUSED")

    def test_env_default_scenario(self):
        os.environ["MOCK_SCENARIO"] = "failure"
        try:
            self.assertError(self.call(), "READ_TRACE_EXTRACTION_FAILED")
        finally:
            del os.environ["MOCK_SCENARIO"]

    def test_validation_errors(self):
        cases = [
            ({"layout": None}, "LAYOUT_MISSING"),
            ({"layout": "4x3"}, "LAYOUT_UNSUPPORTED"),
            ({"calibration": {"speed_mm_s": 30, "gain_mm_mv": 10}}, "CALIBRATION_INVALID"),
            ({"calibration": {"speed_mm_s": 25, "gain_mm_mv": "10"}}, "CALIBRATION_INVALID"),
            ({"image": None}, "IMAGE_MISSING"),
            ({"image": {"bucket": "images"}}, "IMAGE_MISSING"),
            ({"image": {"bucket": "images", "key": "nao-existe.jpg"}}, "IMAGE_NOT_FOUND"),
            ({"request_id": "abc"}, "REQUEST_ID_INVALID"),
            ({"contract_version": "2.0"}, "UNSUPPORTED_CONTRACT_VERSION"),
            ({"mock": {"scenario": "explode"}}, "MOCK_SCENARIO_INVALID"),
            ({"mock": {"scenario": "failure", "code": "READ_REFUSED_BLURRY"}}, "MOCK_SCENARIO_INVALID"),
        ]
        for overrides, code in cases:
            with self.subTest(code=code, overrides=overrides):
                self.assertError(self.call(**overrides), code)

    def test_validation_runs_before_scenario(self):
        self.assertError(self.call(layout="9x9", mock={"scenario": "refused"}), "LAYOUT_UNSUPPORTED")

    def test_image_content_validation(self):
        self.s3.objects[("images", "doc.gif")] = _image_bytes("GIF")
        self.s3.objects[("images", "lixo.jpg")] = b"nao sou imagem"
        self.s3.objects[("images", "mini.png")] = _image_bytes("PNG", (400, 300))
        self.s3.objects[("images", "foto.webp")] = _image_bytes("WEBP")
        for key, code in (("doc.gif", "IMAGE_FORMAT_UNSUPPORTED"), ("lixo.jpg", "IMAGE_UNREADABLE"),
                          ("mini.png", "IMAGE_TOO_SMALL")):
            with self.subTest(key=key):
                self.assertError(self.call(image={"bucket": "images", "key": key}), code)
        self.assertEqual(self.call(image={"bucket": "images", "key": "foto.webp"})["status"], "OK")

    def test_not_a_dict_payload(self):
        self.assertError(reading.handler(["x"], None), "INVALID_PAYLOAD")

    def test_unexpected_exception_is_internal_error(self):
        class Broken(FakeS3):
            def head_object(self, Bucket, Key):
                raise RuntimeError("boom")
        reading.s3 = Broken()
        self.assertError(self.call(), "INTERNAL_ERROR")


class InterpretationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        s3 = FakeS3()
        s3.objects[("images", "foto.jpg")] = _image_bytes()
        reading.s3 = s3
        read = reading.handler({"request_id": str(uuid.uuid4()), "layout": "3x4+1",
                                "image": {"bucket": "images", "key": "foto.jpg"}}, None)
        cls.signal = read["result"]  # saída da leitura entra direto na interpretação

    def call(self, **overrides):
        event = {"contract_version": "1.0", "request_id": str(uuid.uuid4()),
                 "patient": {"sex": "F"}, "signal": self.signal}
        event.update(overrides)
        return interpretation.handler(event, None)

    def assertError(self, response, code):
        self.assertEqual(response["status"], "ERROR", response["error"])
        self.assertEqual(response["error"]["code"], code)
        self.assertIsNone(response["result"])

    def test_success_summary_and_details(self):
        response = self.call()
        self.assertEqual(response["status"], "OK", response["warnings"])
        summary, details = response["result"]["summary"], response["result"]["details"]
        self.assertEqual(summary["classification"], "normal")
        self.assertEqual(summary["heart_rate_bpm"], 72)
        self.assertIn("Ritmo sinusal", summary["key_findings"])
        self.assertEqual(len(details["per_lead"]), 12)
        self.assertIn("NORMAL_AXIS", {f["code"] for f in details["findings"]})
        self.assertTrue(response["result"]["disclaimer"])

    def test_sex_changes_thresholds(self):
        female = self.call(patient={"sex": "F"})["result"]["details"]["criteria"]
        male = self.call(patient={"sex": "M"})["result"]["details"]["criteria"]
        self.assertIn("feminino", female[0]["reference"])
        self.assertIn("450", male[0]["reference"])
        self.assertNotEqual(female[1]["reference"], male[1]["reference"])

    def test_success_with_warnings(self):
        response = self.call(mock={"scenario": "success_with_warnings"})
        self.assertEqual(response["status"], "OK_WITH_WARNINGS")
        self.assertEqual({w["code"] for w in response["warnings"]}, {"LEAD_LOW_QUALITY", "MEASUREMENT_UNRELIABLE"})
        self.assertEqual(response["result"]["details"]["measurements"]["qt_ms"]["confidence"], "low")

    def test_missing_leads_warn(self):
        signal = {"sampling_rate_hz": 500, "leads": [l for l in self.signal["leads"] if l["name"] == "II"]}
        response = self.call(signal=signal)
        self.assertEqual(response["status"], "OK_WITH_WARNINGS")
        self.assertEqual(len([w for w in response["warnings"] if w["code"] == "LEAD_MISSING"]), 11)

    def test_failure_codes(self):
        self.assertError(self.call(mock={"scenario": "failure"}), "INTERP_NO_QRS_DETECTED")
        self.assertError(self.call(mock={"scenario": "failure", "code": "INTERP_SIGNAL_TOO_NOISY"}),
                         "INTERP_SIGNAL_TOO_NOISY")
        self.assertError(self.call(mock={"scenario": "refused"}), "MOCK_SCENARIO_INVALID")

    def test_flat_signal_has_no_qrs(self):
        flat = {"sampling_rate_hz": 500, "leads": [{"name": "II", "samples": [0.0] * 1000}]}
        self.assertError(self.call(signal=flat), "INTERP_NO_QRS_DETECTED")

    def test_validation_errors(self):
        cases = [
            ({"patient": None}, "SEX_MISSING"),
            ({"patient": {"sex": "feminino"}}, "SEX_INVALID"),
            ({"signal": None}, "SIGNAL_MISSING"),
            ({"signal": "x"}, "SIGNAL_INVALID"),
            ({"signal": {"sampling_rate_hz": 500, "leads": []}}, "SIGNAL_MISSING"),
            ({"signal": {"sampling_rate_hz": 50, "leads": [{"name": "II", "samples": [0] * 600}]}}, "SIGNAL_SAMPLING_RATE_INVALID"),
            ({"signal": {"sampling_rate_hz": 500, "units": "uV", "leads": [{"name": "II", "samples": [0] * 600}]}}, "SIGNAL_INVALID"),
            ({"signal": {"sampling_rate_hz": 500, "leads": [{"name": "V7", "samples": [0] * 600}]}}, "SIGNAL_LEAD_UNKNOWN"),
            ({"signal": {"sampling_rate_hz": 500, "leads": [{"name": "II", "samples": [0, "a"]}]}}, "SIGNAL_INVALID"),
            ({"signal": {"sampling_rate_hz": 500, "leads": [{"name": "II", "samples": [0.1] * 100}]}}, "SIGNAL_TOO_SHORT"),
            ({"request_id": None}, "REQUEST_ID_INVALID"),
        ]
        for overrides, code in cases:
            with self.subTest(code=code):
                self.assertError(self.call(**overrides), code)


class CatalogTests(unittest.TestCase):
    def test_every_error_has_category_and_message(self):
        for code, (category, retryable, message) in ERRORS.items():
            self.assertIn(category, ("validation", "refused", "processing", "internal"), code)
            self.assertIsInstance(retryable, bool)
            self.assertTrue(message)

    def test_contract_doc_lists_every_code(self):
        doc = open(os.path.join(os.path.dirname(ROOT), "CONTRATO_LAMBDAS.md"), encoding="utf-8").read()
        missing = [code for code in [*ERRORS, *WARNINGS] if f"`{code}`" not in doc]
        self.assertEqual(missing, [], "códigos ausentes em CONTRATO_LAMBDAS.md")


if __name__ == "__main__":
    unittest.main()
