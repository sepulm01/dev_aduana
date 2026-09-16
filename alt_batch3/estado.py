#!/usr/bin/env python3
"""Estado global de alt_batch2: esquema de estado.db, constantes del
pipeline continuo y candado de GPU compartida."""
import calendar
import datetime
import fcntl
import os
import sqlite3

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "estado.db")
LLEGADA = os.path.join(BASE, "llegada")
VIDEOS = os.path.join(BASE, "videos")
PROCESADOS = os.path.join(BASE, "procesados")
FOTOS = os.path.join(BASE, "fotos")
REGISTROS = os.path.join(BASE, "registros")
FUENTE_VIAJES = "/var/www/dev_aduana/alt_batch/videos"

GAP_CONTINUIDAD = 20.0
UMBRAL_ESTACIONADO = 90.0
TOL_MATCH = 15.0
CONF_SELLO = 0.6
MIN_VOTOS_CODIGO = 2

PRIO = {"strict": 3, "repaired": 2, "raw": 1}
CLS_TEXTO = {0: "CON SELLO", 1: "SIN SELLO", -1: "sin identificar"}

ETAPAS = {
    "nuevo": ("proxy", "detectar"),
    "detectar": ("detectar", "ocr"),
    "ocr": ("ocr", "agrupar"),
    "agrupar": ("agrupar", "listo"),
}


def parse_nombre(nombre):
    """'cam1_20260901_144704' -> (camara, ts_unix)."""
    camara = 1 if nombre.startswith("cam1_") else 2
    ts = nombre.split("_", 1)[1]
    dt = datetime.datetime.strptime(ts, "%Y%m%d_%H%M%S")
    return camara, calendar.timegm(dt.timetuple())


def conectar():
    return sqlite3.connect(DB_PATH, timeout=30)


def crear_tablas():
    conn = conectar()
    cur = conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("""CREATE TABLE IF NOT EXISTS segmentos (
        nombre TEXT PRIMARY KEY, camara INTEGER, ts INTEGER,
        estado TEXT DEFAULT 'nuevo', en_proceso INTEGER DEFAULT 0,
        intentos INTEGER DEFAULT 0, error TEXT,
        llegada_at REAL, fin_at REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pasadas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        camara INTEGER,
        t_inicio REAL, t_fin REAL,
        estado TEXT DEFAULT 'abierta',
        estacionada INTEGER DEFAULT 0,
        consolidada INTEGER DEFAULT 0,
        camion_id INTEGER)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pasada_rangos (
        pasada_id INTEGER, video TEXT, idx INTEGER,
        inicio INTEGER, fin INTEGER,
        PRIMARY KEY (video, idx))""")
    cur.execute("""CREATE TABLE IF NOT EXISTS camiones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        t_inicio REAL, t_fin REAL,
        codigo TEXT, tier TEXT, fuente TEXT, duda_codigo INTEGER,
        seal3 INTEGER, seal3_conf REAL, seal3_veredicto TEXT,
        duda_sello INTEGER,
        foto_cam1 TEXT, foto_cam2 TEXT,
        registro TEXT, parcial INTEGER DEFAULT 0,
        creado_at REAL)""")
    cur.execute("""CREATE TABLE IF NOT EXISTS pasada_subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pasada_id INTEGER, camara INTEGER,
        t_inicio REAL, t_fin REAL,
        codigo TEXT, tier TEXT, peso INTEGER,
        fr TEXT,
        consolidada INTEGER DEFAULT 0, camion_id INTEGER)""")
    conn.commit()
    conn.close()


class FileLock:
    """Candado de archivo entre procesos (fcntl)."""

    def __init__(self, nombre):
        self.path = os.path.join(BASE, f".{nombre}.lock")

    def __enter__(self):
        self.fh = open(self.path, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


class GpuLock(FileLock):
    """Candado exclusivo de GPU compartida."""

    def __init__(self):
        super().__init__("gpu")
