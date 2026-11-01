"""坐标系转换的测试。

重点验证可量化的性质，而不是只测"函数能跑"。
"""

from __future__ import annotations

import pytest

from core.coords import gcj02_to_wgs84, out_of_china, wgs84_to_gcj02

# 北京天安门附近的 WGS84 坐标
BEIJING_WGS_LON, BEIJING_WGS_LAT = 116.3975, 39.9087


def test_wgs84_to_gcj02_offsets_inside_china():
    """国内坐标必须被偏移，且偏移量在合理量级（几十到几百米）。"""
    gcj_lon, gcj_lat = wgs84_to_gcj02(BEIJING_WGS_LON, BEIJING_WGS_LAT)

    assert (gcj_lon, gcj_lat) != (BEIJING_WGS_LON, BEIJING_WGS_LAT), "国内坐标应被偏移"

    # 换算成米：1 度纬度约 111km，1 度经度在北京约 85km
    d_lat_m = abs(gcj_lat - BEIJING_WGS_LAT) * 111_000
    d_lon_m = abs(gcj_lon - BEIJING_WGS_LON) * 85_000
    assert 100 < d_lat_m < 1000, f"纬度偏移 {d_lat_m:.0f} 米，超出常见范围"
    assert 100 < d_lon_m < 1000, f"经度偏移 {d_lon_m:.0f} 米，超出常见范围"


def test_gcj02_to_wgs84_round_trip_is_close():
    """往返转换误差应远小于偏移量本身，否则说明反解不对。"""
    gcj_lon, gcj_lat = wgs84_to_gcj02(BEIJING_WGS_LON, BEIJING_WGS_LAT)
    back_lon, back_lat = gcj02_to_wgs84(gcj_lon, gcj_lat)

    d_lat_m = abs(back_lat - BEIJING_WGS_LAT) * 111_000
    d_lon_m = abs(back_lon - BEIJING_WGS_LON) * 85_000
    # 这个近似反解通常能到 1 米内，放宽到 10 米仍足以证明正确
    assert d_lat_m < 10, f"往返纬度误差 {d_lat_m:.2f} 米"
    assert d_lon_m < 10, f"往返经度误差 {d_lon_m:.2f} 米"


@pytest.mark.parametrize(
    "lon,lat",
    [
        (-74.0060, 40.7128),   # 纽约
        (139.6917, 35.6895),   # 东京
        (2.3522, 48.8566),     # 巴黎
        (151.2093, -33.8688),  # 悉尼
    ],
)
def test_overseas_coordinates_are_not_offset(lon: float, lat: float):
    """境外坐标不能做偏移，否则会把位置算到别处。"""
    assert out_of_china(lon, lat) is True
    assert wgs84_to_gcj02(lon, lat) == (lon, lat)
    assert gcj02_to_wgs84(lon, lat) == (lon, lat)


def test_out_of_china_boundary_sanity():
    """国内几个主要城市都应判定为"在国内"。"""
    for lon, lat in [(116.41, 39.92), (121.47, 31.23), (113.26, 23.13), (104.07, 30.67)]:
        assert out_of_china(lon, lat) is False, f"({lon}, {lat}) 应判定为国内"
