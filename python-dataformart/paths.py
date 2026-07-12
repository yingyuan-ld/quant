# -*- coding: utf-8 -*-
"""python-dataformart 公共路径（JQData 仅作数据存储）。"""

from pathlib import Path

QUANT_ROOT = Path(__file__).resolve().parent.parent
PKG_ROOT = Path(__file__).resolve().parent
JQDATA_ROOT = QUANT_ROOT / "JQData"

DAILY_DIR = JQDATA_ROOT / "daily"
LOGS_DIR = JQDATA_ROOT / "logs"

INGEST_DIR = PKG_ROOT / "入库"
READ_DIR = PKG_ROOT / "读取"
