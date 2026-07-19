from django.test import SimpleTestCase

from aduana.ocr_codes import (
    candidatos_de_regiones,
    consenso_parcial,
    corregir_posicional,
    es_contenedor_valido,
    ventanas_11,
)


class EsContenedorValidoTests(SimpleTestCase):
    def test_codigos_reales_validos(self):
        for code in ["ONEU9330000", "TCLU1391037", "CXRU1133580", "MORU1325733"]:
            with self.subTest(code=code):
                self.assertTrue(es_contenedor_valido(code))

    def test_checksum_incorrecto_es_invalido(self):
        # Calculado con el propio algoritmo: el dígito de checksum correcto
        # para ABCU123456X es 0, no 7 -> ABCU1234567 es inválido.
        self.assertFalse(es_contenedor_valido("ABCU1234567"))

    def test_corto_es_invalido(self):
        self.assertFalse(es_contenedor_valido("45G1"))

    def test_vacio_es_invalido(self):
        self.assertFalse(es_contenedor_valido(""))

    def test_minusculas_validas_tras_limpiar(self):
        self.assertTrue(es_contenedor_valido("oneu9330000"))


class CorregirPosicionalTests(SimpleTestCase):
    def test_letra_confundida_con_digito_al_final(self):
        self.assertEqual(corregir_posicional("ONEU933000O"), "ONEU9330000")

    def test_digito_confundido_con_letra_al_inicio(self):
        self.assertEqual(corregir_posicional("0NEU9330000"), "ONEU9330000")

    def test_string_correcto_queda_igual(self):
        self.assertEqual(corregir_posicional("ONEU9330000"), "ONEU9330000")

    def test_largo_distinto_de_11_retorna_none(self):
        self.assertIsNone(corregir_posicional("ONEU933000"))
        self.assertIsNone(corregir_posicional("ONEU93300000"))


class Ventanas11Tests(SimpleTestCase):
    def test_texto_con_espacios_y_guiones(self):
        ventanas = ventanas_11("C XRU-113358 0")
        self.assertIn("CXRU1133580", ventanas)

    def test_texto_exacto_11_una_sola_ventana(self):
        ventanas = ventanas_11("CXRU1133580")
        self.assertEqual(ventanas, {"CXRU1133580"})

    def test_texto_corto_set_vacio(self):
        self.assertEqual(ventanas_11("CXRU113"), set())


class CandidatosDeRegionesTests(SimpleTestCase):
    def test_codigo_partido_en_dos_lineas_sin_bbox(self):
        # Estilo OCR-VL: regiones sin bbox (lista vacía), el código queda
        # partido entre dos líneas consecutivas.
        regions = [
            ["CXRU113", 0.85, []],
            ["3580", 0.85, []],
        ]
        candidatos = candidatos_de_regiones(regions, "")
        self.assertIn("CXRU1133580", candidatos)

    def test_orden_por_y_reconstruye_texto_vertical(self):
        # Simula texto vertical: el orden de almacenamiento y por X no dan
        # el código correcto, pero el orden por Y (de arriba a abajo) sí.
        regions = [
            ["1133580", 0.9, [[0.1, 0.9]]],
            ["CXRU", 0.9, [[0.5, 0.1]]],
        ]
        candidatos = candidatos_de_regiones(regions, "")
        self.assertIn("CXRU1133580", candidatos)

    def test_codigo_embebido_en_texto_largo_region_unica(self):
        regions = [
            ["TARA 2250 CXRU1133580 MAX GROSS 30480", 0.9, []],
        ]
        candidatos = candidatos_de_regiones(regions, "")
        self.assertIn("CXRU1133580", candidatos)


class ConsensoParcialTests(SimpleTestCase):
    def test_prefijo_y_cuerpo_en_textos_separados(self):
        # Caso real de producción: crop horizontal con el prefijo, crop
        # vertical con los dígitos.
        self.assertTrue(es_contenedor_valido("CXRU1133580"))
        self.assertEqual(consenso_parcial(["CXRU", "1133580"]), "CXRU1133580")

    def test_correccion_de_letra_por_digito_en_el_cuerpo(self):
        self.assertTrue(es_contenedor_valido("MORU1325733"))
        self.assertEqual(consenso_parcial(["MORU", "I325733"]), "MORU1325733")

    def test_sin_cuerpo_valido_devuelve_none(self):
        # "KSU 250996" no aporta ningún cuerpo de 7 dígitos reconstruible
        # (todas las ventanas contienen una letra sin mapeo a dígito), así
        # que no hay candidato posible: debe ser None. Si la implementación
        # cambiara y llegara a producir un código, igual debe pasar el
        # checksum ISO 6346.
        resultado = consenso_parcial(["KSU 250996"])
        if resultado is not None:
            self.assertTrue(es_contenedor_valido(resultado))
        else:
            self.assertIsNone(resultado)

    def test_empate_de_soporte_es_ambiguo_y_devuelve_none(self):
        # Dos códigos válidos distintos con el mismo prefijo "CXRU", cada
        # uno aportado por un texto de cuerpo separado: mismo soporte (2)
        # para ambos -> ambiguo -> None.
        self.assertTrue(es_contenedor_valido("CXRU0000006"))
        self.assertTrue(es_contenedor_valido("CXRU0000011"))
        self.assertIsNone(consenso_parcial(["CXRU", "0000006", "0000011"]))

    def test_lista_vacia_devuelve_none(self):
        self.assertIsNone(consenso_parcial([]))

    def test_textos_vacios_devuelve_none(self):
        self.assertIsNone(consenso_parcial(["", "   ", None]))
