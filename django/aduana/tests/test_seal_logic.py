from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from aduana.models import ContainerDetection, ContainerEvent
from aduana.tasks import _finalize_event
from devices.models import Device


class FinalizeEventSealLogicTests(TestCase):
    """
    Regresión para el bug donde class_id=2 (cont data / placa de datos) se
    contaba erróneamente como "sin_sello". El mapeo correcto del modelo
    (aduana/models.py CLASS_CHOICES) es:
        0 = con_sello
        1 = sin_sello
        2 = cont data (placa de datos del contenedor)
        3 = container cod (código del contenedor, para OCR)
    Solo las clases 0 y 1 son señales de sello; 2 y 3 no deben aportar a
    con_sello_count/sin_sello_count.
    """

    @classmethod
    def setUpTestData(cls):
        cls.device = Device.objects.create(name="cam-test", host="10.0.0.1")

    def _make_event(self, start=None):
        start = start or timezone.now()
        return ContainerEvent.objects.create(
            seal_status="processing",
            timestamp_start=start,
        )

    def _make_detection(self, event, class_id, offset_seconds):
        return ContainerDetection.objects.create(
            event=event,
            device=self.device,
            source_id=0,
            class_id=class_id,
            object_id=0,
            frame_num=0,
            confidence=0.9,
            bbox_left=0.0,
            bbox_top=0.0,
            bbox_width=10.0,
            bbox_height=10.0,
            roi_name="",
            timestamp=event.timestamp_start + timedelta(seconds=offset_seconds),
            crop="crops/test.jpg",
        )

    def test_con_sello_mayoritario(self):
        event = self._make_event()
        for i in range(5):
            self._make_detection(event, class_id=0, offset_seconds=i * 0.5)
        for i in range(2):
            self._make_detection(event, class_id=1, offset_seconds=5 * 0.5 + i * 0.5)

        _finalize_event(event)
        event.refresh_from_db()

        self.assertEqual(event.seal_status, "con_sello")
        self.assertAlmostEqual(event.seal_confidence, 5 / 7)

    def test_sin_sello_mayoritario(self):
        event = self._make_event()
        for i in range(2):
            self._make_detection(event, class_id=0, offset_seconds=i * 0.5)
        for i in range(5):
            self._make_detection(event, class_id=1, offset_seconds=2 * 0.5 + i * 0.5)

        _finalize_event(event)
        event.refresh_from_db()

        self.assertEqual(event.seal_status, "sin_sello")
        self.assertAlmostEqual(event.seal_confidence, 5 / 7)

    def test_solo_cont_data_y_container_cod_es_indeterminado(self):
        """
        Regresión clave: antes del fix, class_id=2 (cont data) se contaba
        como sin_sello. Un evento con solo detecciones de clase 2 y 3 (sin
        ninguna detección real de sello 0/1) debe quedar "indeterminado",
        no "sin_sello".
        """
        event = self._make_event()
        for i in range(4):
            self._make_detection(event, class_id=2, offset_seconds=i * 0.5)
        for i in range(3):
            self._make_detection(event, class_id=3, offset_seconds=4 * 0.5 + i * 0.5)

        _finalize_event(event)
        event.refresh_from_db()

        self.assertEqual(event.seal_status, "indeterminado")
        self.assertEqual(event.seal_confidence, 0.0)

    def test_evento_sin_detecciones_de_sello_es_indeterminado(self):
        event = self._make_event()
        for i in range(3):
            self._make_detection(event, class_id=3, offset_seconds=i * 0.5)

        _finalize_event(event)
        event.refresh_from_db()

        self.assertEqual(event.seal_status, "indeterminado")
        self.assertEqual(event.seal_confidence, 0.0)
