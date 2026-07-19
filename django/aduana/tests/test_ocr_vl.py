import os
import tempfile
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from aduana.tasks import _run_ocr_vl


def _resp(status_code=200, text=""):
    m = MagicMock()
    m.status_code = status_code
    m.json.return_value = {"text": text}
    return m


class RunOcrVlTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self._tmp.write(b"fake-image-bytes")
        self._tmp.close()
        self.image_path = self._tmp.name
        self.addCleanup(lambda: os.path.exists(self.image_path) and os.remove(self.image_path))

    @patch("requests.post")
    def test_vertical_true_llama_spotting_primero(self, mock_post):
        mock_post.side_effect = [_resp(text=""), _resp(text="")]
        _run_ocr_vl(self.image_path, vertical=True)
        first_url = mock_post.call_args_list[0].args[0]
        self.assertTrue(first_url.endswith("/spotting"))

    @patch("requests.post")
    def test_vertical_false_llama_ocr_primero(self, mock_post):
        mock_post.side_effect = [_resp(text=""), _resp(text="")]
        _run_ocr_vl(self.image_path, vertical=False)
        first_url = mock_post.call_args_list[0].args[0]
        self.assertTrue(first_url.endswith("/ocr"))

    @patch("requests.post")
    def test_primer_modo_codigo_valido_no_llama_segundo(self, mock_post):
        mock_post.side_effect = [_resp(text="MORU1325733")]
        result = _run_ocr_vl(self.image_path, vertical=False)
        self.assertEqual(mock_post.call_count, 1)
        self.assertEqual(result["text"], "MORU1325733")
        self.assertIn(["MORU1325733", 0.85, []], result["regions"])

    @patch("requests.post")
    def test_primer_truncado_segundo_valido_incluye_ambas_regions(self, mock_post):
        mock_post.side_effect = [
            _resp(text="XRU113358"),
            _resp(text="MORU1325733"),
        ]
        result = _run_ocr_vl(self.image_path, vertical=False)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result["text"], "MORU1325733")
        lines = [r[0] for r in result["regions"]]
        self.assertIn("MORU1325733", lines)
        self.assertIn("XRU113358", lines)
        # El segundo modo (principal) va antes que las líneas del primero.
        self.assertLess(lines.index("MORU1325733"), lines.index("XRU113358"))

    @patch("requests.post")
    def test_ambos_vacios_retorna_none(self, mock_post):
        mock_post.side_effect = [_resp(text=""), _resp(text="")]
        result = _run_ocr_vl(self.image_path, vertical=False)
        self.assertIsNone(result)

    @patch("requests.post")
    def test_primer_modo_status_500_segundo_valido(self, mock_post):
        mock_post.side_effect = [
            _resp(status_code=500),
            _resp(text="MORU1325733"),
        ]
        result = _run_ocr_vl(self.image_path, vertical=False)
        self.assertEqual(mock_post.call_count, 2)
        self.assertEqual(result["text"], "MORU1325733")

    @patch("requests.post")
    def test_ambos_sin_codigo_valido_combina_regions_sin_duplicados(self, mock_post):
        mock_post.side_effect = [
            _resp(text="TARA 2250\nMAX GROSS"),
            _resp(text="MAX GROSS\nNET 27300"),
        ]
        result = _run_ocr_vl(self.image_path, vertical=False)
        self.assertEqual(mock_post.call_count, 2)
        lines = [r[0] for r in result["regions"]]
        self.assertEqual(lines, ["TARA 2250", "MAX GROSS", "NET 27300"])
        self.assertEqual(result["text"], "TARA 2250")
