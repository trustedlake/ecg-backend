from types import SimpleNamespace
from unittest.mock import patch
from django.test import SimpleTestCase, override_settings
from exams import services

class SingleLambdaTests(SimpleTestCase):
    @override_settings(AWS_LAMBDA_ECG_ANALYSER="new-lambda", AWS_IMAGES_BUCKET="img", AWS_RESULTS_BUCKET="out", PRESIGN_URL_EXPIRES=60)
    @patch("exams.services.original_image_url", return_value="original")
    @patch("exams.services.overlay_urls", return_value={"overlay_trace_url":None,"overlay_signal_url":None})
    @patch("exams.services.invoke_lambda")
    def test_rejection_does_not_fabricate_signals(self, invoke, overlays, original):
        invoke.return_value={"status":"REJECTED_LOW_TRACE_GRID_CONTRAST", "warnings":["low contrast"],"leads":[],"artifacts":{"result":"ecg-analyser/attempt/initial/result.json"}}
        a=SimpleNamespace(id="attempt", imagem_s3_key="image.jpg", calibracao={"speed_mm_s":25, "gain_mm_mv":10}, save=lambda: None)
        e=SimpleNamespace(id="exam")
        with patch("exams.services.presign_get_url", return_value="result"):
            r=services.run_phase_a(a,e)
        self.assertEqual(r["analyser_status"], "REJECTED_LOW_TRACE_GRID_CONTRAST")
        self.assertEqual(a.fidelidade,"ruim")
        self.assertEqual(a.sinais["signals_s3_key"], None)
        self.assertEqual(invoke.call_args.args[0], "new-lambda")
