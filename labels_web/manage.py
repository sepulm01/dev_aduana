#!/usr/bin/env python3
"""Clasificador de crops de sellos — sitio dinamico en aduana.streetflow.cl.

Los usuarios clasifican cada crop de sello (keypoint 3) como:
con sello / sin sello / duda. El export genera un ZIP con dos
directorios (con_sello/ y sin_sello/)."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
