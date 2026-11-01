"""坐标系转换：WGS84 ↔ GCJ-02。

和风天气给 WGS84、高德要 GCJ-02，直接混用会偏几十到几百米；算法是公开近似实现，纯数学无依赖。
"""

from __future__ import annotations

import math
from typing import Final

#: GCJ-02 使用的椭球长半轴（克拉索夫斯基椭球）
_A: Final[float] = 6378245.0
#: 椭球偏心率平方
_EE: Final[float] = 0.00669342162296594323

#: 中国大致经纬度范围。境外不做偏移，否则会把坐标算飞。
_LON_MIN, _LON_MAX = 72.004, 137.8347
_LAT_MIN, _LAT_MAX = 0.8293, 55.8271


def out_of_china(lon: float, lat: float) -> bool:
    """判断是否在中国境外（境外无需做 GCJ-02 偏移）。"""
    return not (_LON_MIN < lon < _LON_MAX and _LAT_MIN < lat < _LAT_MAX)


def _transform_lat(x: float, y: float) -> float:
    ret = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
    )
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """WGS84 -> GCJ-02。返回 (经度, 纬度)。"""
    if out_of_china(lon, lat):
        return lon, lat

    d_lat = _transform_lat(lon - 105.0, lat - 35.0)
    d_lon = _transform_lon(lon - 105.0, lat - 35.0)
    rad_lat = lat / 180.0 * math.pi
    magic = math.sin(rad_lat)
    magic = 1 - _EE * magic * magic
    sqrt_magic = math.sqrt(magic)

    d_lat = (d_lat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrt_magic) * math.pi)
    d_lon = (d_lon * 180.0) / (_A / sqrt_magic * math.cos(rad_lat) * math.pi)
    return lon + d_lon, lat + d_lat


def gcj02_to_wgs84(lon: float, lat: float) -> tuple[float, float]:
    """GCJ-02 -> WGS84（用一次逼近反解，精度足够）。返回 (经度, 纬度)。"""
    if out_of_china(lon, lat):
        return lon, lat

    # 先正向偏移一次，再按差值反推
    g_lon, g_lat = wgs84_to_gcj02(lon, lat)
    return lon * 2 - g_lon, lat * 2 - g_lat
