from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from aduana.models import ContainerDetection, ContainerEvent
from aduana.tasks import (
    _find_temporal_clusters,
    _split_event,
    _try_merge_event,
    aggregate_ocr_results,
)
from devices.models import Device


class AggregateOcrResultsTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = Device.objects.create(name="cam-test", host="10.0.0.1")

    def _make_event(self, start=None):
        start = start or timezone.now()
        return ContainerEvent.objects.create(
            seal_status="processing",
            timestamp_start=start,
        )

    def _make_detection(
        self,
        event,
        class_id,
        offset_seconds,
        source_id=0,
        ocr_texts=None,
        ocr_text="",
        ocr_confidence=None,
        ocr_processed=False,
        dominant_color=None,
    ):
        color = dominant_color or (None, None, None)
        return ContainerDetection.objects.create(
            event=event,
            device=self.device,
            source_id=source_id,
            class_id=class_id,
            object_id=0,
            frame_num=0,
            confidence=0.9,
            bbox_left=0.0,
            bbox_top=0.0,
            bbox_width=10.0,
            bbox_height=10.0,
            dominant_color_h=color[0],
            dominant_color_s=color[1],
            dominant_color_v=color[2],
            roi_name="",
            timestamp=event.timestamp_start + timedelta(seconds=offset_seconds),
            crop="crops/test.jpg",
            ocr_texts=ocr_texts or [],
            ocr_text=ocr_text,
            ocr_confidence=ocr_confidence,
            ocr_processed=ocr_processed,
        )

    def test_una_sola_deteccion_procesada_obtiene_codigo(self):
        """Regresión del mínimo de 2 detecciones: con 1 sola basta."""
        event = self._make_event()
        self._make_detection(
            event,
            class_id=3,
            offset_seconds=0,
            ocr_texts=[["CXRU1133580", 0.9, []]],
            ocr_processed=True,
        )

        aggregate_ocr_results(event.id)
        event.refresh_from_db()

        self.assertEqual(event.container_code, "CXRU1133580")

    def test_gana_el_codigo_mayoritario(self):
        event = self._make_event()
        # 2 detecciones votan CXRU1133580
        self._make_detection(
            event, class_id=3, offset_seconds=0,
            ocr_texts=[["CXRU1133580", 0.9, []]], ocr_processed=True,
        )
        self._make_detection(
            event, class_id=3, offset_seconds=0.5,
            ocr_texts=[["CXRU1133580", 0.9, []]], ocr_processed=True,
        )
        # 1 deteccion vota TCLU1391037
        self._make_detection(
            event, class_id=3, offset_seconds=1.0,
            ocr_texts=[["TCLU1391037", 0.9, []]], ocr_processed=True,
        )

        aggregate_ocr_results(event.id)
        event.refresh_from_db()

        self.assertEqual(event.container_code, "CXRU1133580")

    def test_codigo_repetido_dentro_de_una_deteccion_cuenta_una_vez(self):
        """
        Una detección que repite el mismo código en varias regiones y en
        ocr_text debe aportar 1 solo voto, no 3. Se verifica contra otro
        código que sí junta 2 votos reales de 2 detecciones distintas.
        """
        event = self._make_event()
        # Detección única, pero con el código repetido 3 veces (2 regiones + ocr_text)
        self._make_detection(
            event, class_id=3, offset_seconds=0,
            ocr_texts=[
                ["CXRU1133580", 0.9, []],
                ["CXRU1133580", 0.9, []],
            ],
            ocr_text="CXRU1133580",
            ocr_confidence=0.9,
            ocr_processed=True,
        )
        # 2 detecciones distintas votando TCLU1391037
        self._make_detection(
            event, class_id=3, offset_seconds=0.5,
            ocr_texts=[["TCLU1391037", 0.9, []]], ocr_processed=True,
        )
        self._make_detection(
            event, class_id=3, offset_seconds=1.0,
            ocr_texts=[["TCLU1391037", 0.9, []]], ocr_processed=True,
        )

        aggregate_ocr_results(event.id)
        event.refresh_from_db()

        self.assertEqual(event.container_code, "TCLU1391037")

    def test_split_event_dispara_agregacion_para_evento_nuevo(self):
        """
        _split_event debe encolar aggregate_ocr_results para el evento
        nuevo. Con CELERY_TASK_ALWAYS_EAGER, basta crear el escenario de
        split (2 clusters temporales con colores distintos) y verificar
        que el evento nuevo quede con container_code.
        """
        event = self._make_event()

        # Cluster 1 (se queda en el evento original): 3 detecciones, mismo color.
        for i in range(3):
            self._make_detection(
                event, class_id=0, offset_seconds=i * 0.5,
                dominant_color=(0.1, 0.5, 0.5),
            )

        # Cluster 2 (se separa a un evento nuevo): gap > 3s, color distinto,
        # incluye una detección clase 3 con código OCR válido.
        base = 3 * 0.5 + 4.0
        self._make_detection(
            event, class_id=0, offset_seconds=base,
            dominant_color=(0.9, 0.5, 0.5),
        )
        self._make_detection(
            event, class_id=0, offset_seconds=base + 0.5,
            dominant_color=(0.9, 0.5, 0.5),
        )
        self._make_detection(
            event, class_id=3, offset_seconds=base + 1.0,
            ocr_texts=[["CXRU1133580", 0.9, []]],
            ocr_processed=True,
        )

        detections = ContainerDetection.objects.filter(event=event)
        clusters = _find_temporal_clusters(detections)
        self.assertEqual(len(clusters), 2)

        _split_event(event, clusters)

        new_event = ContainerEvent.objects.exclude(id=event.id).get()
        self.assertEqual(new_event.container_code, "CXRU1133580")

    def test_consenso_parcial_reconstruye_codigo_desde_lecturas_partidas(self):
        """
        Ninguna detección por sí sola trae un código completo: una aporta
        solo el prefijo de letras (crop horizontal), otra solo los dígitos
        (crop vertical). El consenso parcial debe reconstruirlo.
        """
        event = self._make_event()
        self._make_detection(
            event, class_id=3, offset_seconds=0,
            ocr_texts=[["CXRU", 0.9, []]], ocr_processed=True,
        )
        self._make_detection(
            event, class_id=3, offset_seconds=0.5,
            ocr_texts=[["1133580", 0.9, []]], ocr_processed=True,
        )

        aggregate_ocr_results(event.id)
        event.refresh_from_db()

        self.assertEqual(event.container_code, "CXRU1133580")

    def test_consenso_parcial_no_pisa_el_voto_directo(self):
        """
        Si alguna detección ya aporta un código completo y válido (voto
        directo), el consenso parcial ni siquiera debe intentarse, aunque
        otras detecciones traigan fragmentos de un código distinto.
        """
        event = self._make_event()
        self._make_detection(
            event, class_id=3, offset_seconds=0,
            ocr_texts=[["TCLU1391037", 0.9, []]], ocr_processed=True,
        )
        # Fragmentos de un código distinto (CXRU1133580) que, si se
        # combinaran, competirían con el voto directo.
        self._make_detection(
            event, class_id=3, offset_seconds=0.5,
            ocr_texts=[["CXRU", 0.9, []]], ocr_processed=True,
        )
        self._make_detection(
            event, class_id=3, offset_seconds=1.0,
            ocr_texts=[["1133580", 0.9, []]], ocr_processed=True,
        )

        aggregate_ocr_results(event.id)
        event.refresh_from_db()

        self.assertEqual(event.container_code, "TCLU1391037")


class TryMergeEventCodeTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.device = Device.objects.create(name="cam-test-merge", host="10.0.0.1")

    def _make_detection(self, event, offset_seconds, dominant_color, class_id=0):
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
            dominant_color_h=dominant_color[0],
            dominant_color_s=dominant_color[1],
            dominant_color_v=dominant_color[2],
            roi_name="",
            timestamp=event.timestamp_start + timedelta(seconds=offset_seconds),
            crop="crops/test.jpg",
        )

    def test_merge_por_mismo_codigo_ignora_diferencia_de_color(self):
        """
        Si prev y event ya tienen el mismo container_code, deben fusionarse
        aunque el color HSV promedio sea muy distinto (el color no importa
        cuando el código ya confirma que es el mismo contenedor).
        """
        now = timezone.now()
        prev = ContainerEvent.objects.create(
            seal_status="con_sello",
            timestamp_start=now - timedelta(seconds=20),
            timestamp_end=now - timedelta(seconds=10),
            container_code="CXRU1133580",
        )
        event = ContainerEvent.objects.create(
            seal_status="processing",
            timestamp_start=now - timedelta(seconds=5),
            container_code="CXRU1133580",
        )
        # Colores muy distintos: por color solo, esto NO se fusionaría.
        self._make_detection(prev, 0, (0.1, 0.5, 0.5))
        self._make_detection(prev, 0.5, (0.1, 0.5, 0.5))
        self._make_detection(event, 0, (0.9, 0.5, 0.5))
        self._make_detection(event, 0.5, (0.9, 0.5, 0.5))

        merged = _try_merge_event(event)

        self.assertTrue(merged)
        self.assertFalse(ContainerEvent.objects.filter(id=event.id).exists())
        prev.refresh_from_db()
        self.assertEqual(prev.container_code, "CXRU1133580")

    def test_merge_desde_aggregate_por_codigo_coincidente(self):
        """
        Un evento ya cerrado con código X; un segundo evento cerrado poco
        después (< MERGE_WINDOW) cuyas detecciones OCR producen el mismo
        código X al agregar: debe terminar fusionado en el primero.
        """
        now = timezone.now()
        first = ContainerEvent.objects.create(
            seal_status="con_sello",
            timestamp_start=now - timedelta(seconds=40),
            timestamp_end=now - timedelta(seconds=30),
            container_code="CXRU1133580",
        )
        second = ContainerEvent.objects.create(
            seal_status="con_sello",
            timestamp_start=now - timedelta(seconds=20),
            timestamp_end=now - timedelta(seconds=10),
        )
        ContainerDetection.objects.create(
            event=second,
            device=self.device,
            source_id=0,
            class_id=3,
            object_id=0,
            frame_num=0,
            confidence=0.9,
            bbox_left=0.0,
            bbox_top=0.0,
            bbox_width=10.0,
            bbox_height=10.0,
            roi_name="",
            timestamp=second.timestamp_start,
            crop="crops/test.jpg",
            ocr_texts=[["CXRU1133580", 0.9, []]],
            ocr_text="",
            ocr_processed=True,
        )

        aggregate_ocr_results(second.id)

        self.assertFalse(ContainerEvent.objects.filter(id=second.id).exists())
        first.refresh_from_db()
        self.assertEqual(first.container_code, "CXRU1133580")
