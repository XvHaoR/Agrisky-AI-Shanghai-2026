"""
Agrisky AI — 空间合规核验（金融级定损底线）

支持多格式边界输入：GeoJSON (.geojson/.json)、Shapefile (.shp)、
GeoPackage (.gpkg)、KML (.kml)，以及直接传入 GeoJSON dict。

核心职责：
    受灾多边形 ∩ 承保红线 = 合规受灾面积
    超出部分一律剔除
"""

from __future__ import annotations

import json
import logging
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger(__name__)

# ── 支持的格式及其文件后缀 ──────────────────────────────────
SUPPORTED_FORMATS = {
    ".geojson": "GeoJSON",
    ".json": "GeoJSON",
    ".shp": "ESRI Shapefile",
    ".gpkg": "GeoPackage",
    ".kml": "KML",
}

MAX_SPATIAL_ZIP_FILES = 256
MAX_SPATIAL_ZIP_MEMBER_BYTES = 100 * 1024 * 1024
MAX_SPATIAL_ZIP_TOTAL_BYTES = 200 * 1024 * 1024


class _UnsafeSpatialZipError(Exception):
    pass


class _NoShapefileInZipError(Exception):
    pass


class _MultipleShapefilesInZipError(Exception):
    pass


def _extract_single_shapefile_zip(archive: zipfile.ZipFile, target_dir: Path) -> Path:
    """Safely extract one SHP dataset; bulk KML/parcel archives are deliberately rejected."""
    members = archive.infolist()
    if len(members) > MAX_SPATIAL_ZIP_FILES:
        raise _UnsafeSpatialZipError(f"ZIP 文件数量超过上限 {MAX_SPATIAL_ZIP_FILES}")
    shp_members = [
        member for member in members
        if not member.is_dir() and PurePosixPath(member.filename.replace("\\", "/")).suffix.lower() == ".shp"
    ]
    if not shp_members:
        raise _NoShapefileInZipError("ZIP 仅支持单个 Shapefile 数据集；批量 KML/多地块压缩包必须先预检、拆分并逐保单确认，不能直接入库")
    if len(shp_members) != 1:
        raise _MultipleShapefilesInZipError("ZIP 中包含多个 .shp，无法确定唯一承保边界；请拆分后逐个登记")

    total_size = 0
    safe_members: list[tuple[zipfile.ZipInfo, Path]] = []
    root = target_dir.resolve()
    for member in members:
        if member.flag_bits & 0x1:
            raise _UnsafeSpatialZipError("ZIP 包含加密文件，不允许导入")
        mode = (member.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise _UnsafeSpatialZipError("ZIP 包含符号链接，不允许导入")
        if member.file_size > MAX_SPATIAL_ZIP_MEMBER_BYTES:
            raise _UnsafeSpatialZipError("ZIP 单个文件解压后超过 100 MB 上限")
        total_size += member.file_size
        if total_size > MAX_SPATIAL_ZIP_TOTAL_BYTES:
            raise _UnsafeSpatialZipError("ZIP 解压总大小超过 200 MB 上限")

        relative = PurePosixPath(member.filename.replace("\\", "/"))
        if relative.is_absolute() or not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
            raise _UnsafeSpatialZipError("ZIP 包含不安全路径")
        destination = (root / Path(*relative.parts)).resolve()
        if not destination.is_relative_to(root):
            raise _UnsafeSpatialZipError("ZIP 包含越界路径")
        safe_members.append((member, destination))

    for member, destination in safe_members:
        if member.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(member) as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output, length=1024 * 1024)

    shp_relative = PurePosixPath(shp_members[0].filename.replace("\\", "/"))
    return root / Path(*shp_relative.parts)


def _detect_format(file_path: str) -> str | None:
    """检测边界文件格式。"""
    path = Path(file_path)
    suffix = path.suffix.lower()
    return SUPPORTED_FORMATS.get(suffix)


def load_boundary(file_path: str) -> dict[str, Any]:
    """从文件加载边界，自动检测格式并转换为 GeoJSON dict。

    支持：.geojson / .json / .shp / .gpkg / .kml

    Returns:
        {
            "status": "success" | "error",
            "geojson": dict,           # 标准 GeoJSON dict
            "source_format": str,      # 原始格式
            "feature_count": int,      # 要素数量
            "crs": str | None,         # 坐标系
        }
    """
    try:
        import geopandas as gpd

        fmt = _detect_format(file_path)
        if fmt is None:
            return {
                "status": "error",
                "error_code": "UNSUPPORTED_FORMAT",
                "error_message": f"不支持的格式: {Path(file_path).suffix}。支持: {', '.join(SUPPORTED_FORMATS.keys())}",
            }

        # 读取文件
        gdf = gpd.read_file(file_path)
        if gdf.empty:
            return {
                "status": "error",
                "error_code": "EMPTY_FILE",
                "error_message": "文件中没有找到几何要素",
            }

        # 统一转为 WGS84 (EPSG:4326)
        crs = str(gdf.crs) if gdf.crs else None
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        # 转为 GeoJSON dict（default=str 兜底 Timestamp 等不可序列化字段）
        geojson_str = gdf.to_json(default=str)
        geojson = json.loads(geojson_str)

        return {
            "status": "success",
            "geojson": geojson,
            "source_format": fmt,
            "feature_count": len(gdf),
            "crs": crs,
        }

    except ImportError:
        return {
            "status": "error",
            "error_code": "GEOPANDAS_NOT_INSTALLED",
            "error_message": "geopandas 未安装。请执行: conda install -c conda-forge geopandas -y",
            "retryable": False,
        }
    except Exception as e:
        logger.error(f"边界文件加载失败: {e}")
        return {
            "status": "error",
            "error_code": "LOAD_ERROR",
            "error_message": str(e),
        }


def load_boundary_from_upload(upload_bytes: bytes, filename: str) -> dict[str, Any]:
    """从上传的原始字节加载边界文件（支持 zip 打包的 SHP）。

    SHP 文件通常由 .shp/.shx/.dbf/.prj 多个文件组成，
    可通过 zip 打包上传。
    """
    try:
        import geopandas as gpd
        import io

        suffix = Path(filename).suffix.lower()

        if suffix == ".zip":
            # 仅允许一个经过路径/体积校验的 SHP 数据集；多地块压缩包不得直接入库。
            with tempfile.TemporaryDirectory() as tmpdir:
                with zipfile.ZipFile(io.BytesIO(upload_bytes) if isinstance(upload_bytes, bytes) else upload_bytes) as zf:
                    shp_path = _extract_single_shapefile_zip(zf, Path(tmpdir))
                gdf = gpd.read_file(str(shp_path))
        else:
            # 单文件：写入临时文件后加载
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
                if isinstance(upload_bytes, bytes):
                    tmp.write(upload_bytes)
                tmp_path = tmp.name
            try:
                gdf = gpd.read_file(tmp_path)
            finally:
                Path(tmp_path).unlink(missing_ok=True)

        if gdf.empty:
            return {
                "status": "error",
                "error_code": "EMPTY_FILE",
                "error_message": "文件中没有找到几何要素",
            }

        crs = str(gdf.crs) if gdf.crs else None
        if gdf.crs and gdf.crs.to_epsg() != 4326:
            gdf = gdf.to_crs("EPSG:4326")

        geojson = json.loads(gdf.to_json(default=str))

        return {
            "status": "success",
            "geojson": geojson,
            "source_format": _detect_format(filename) or Path(filename).suffix,
            "feature_count": len(gdf),
            "crs": crs,
        }

    except _NoShapefileInZipError as e:
        return {"status": "error", "error_code": "NO_SHP_IN_ZIP", "error_message": str(e)}
    except _MultipleShapefilesInZipError as e:
        return {"status": "error", "error_code": "MULTIPLE_SHP_IN_ZIP", "error_message": str(e)}
    except (zipfile.BadZipFile, _UnsafeSpatialZipError) as e:
        return {"status": "error", "error_code": "UNSAFE_OR_INVALID_ZIP", "error_message": str(e)}
    except ImportError:
        return {
            "status": "error",
            "error_code": "GEOPANDAS_NOT_INSTALLED",
            "error_message": "geopandas 未安装。",
        }
    except Exception as e:
        logger.error(f"上传文件加载失败: {e}")
        return {
            "status": "error",
            "error_code": "LOAD_ERROR",
            "error_message": str(e),
        }


def calculate_valid_claim_area(
    damage_geojson: dict,
    insured_geojson: dict,
) -> dict[str, Any]:
    """空间求交：计算承保范围内的合规受灾面积。

    Args:
        damage_geojson:  受灾多边形 GeoJSON dict
        insured_geojson: 承保红线 GeoJSON dict

    Returns:
        {
            "status": "success" | "error",
            "insured_area_mu": float,
            "raw_damage_area_mu": float,
            "valid_damage_area_mu": float,
            "excluded_area_mu": float,
            "damage_ratio": float,
            "clip_log": list[str],
        }
    """
    try:
        import geopandas as gpd
        from shapely.geometry import shape

        # ── 解析 GeoJSON → GeoDataFrame ─────────────────────
        def _gj_to_gdf(gj: dict) -> gpd.GeoDataFrame:
            if gj.get("type") == "FeatureCollection":
                return gpd.GeoDataFrame.from_features(gj["features"], crs="EPSG:4326")
            elif gj.get("type") in ("Polygon", "MultiPolygon"):
                return gpd.GeoDataFrame(geometry=[shape(gj)], crs="EPSG:4326")
            else:
                raise ValueError(f"不支持的 GeoJSON 类型: {gj.get('type')}")

        damage_gdf = _gj_to_gdf(damage_geojson)
        insured_gdf = _gj_to_gdf(insured_geojson)

        # ── 合并多个承保地块（如果有） ─────────────────────
        insured_union = insured_gdf.geometry.union_all()

        # ── 统一转为投影坐标算面积（UTM，米制） ────────────
        centroid = insured_union.centroid
        utm_zone = int((centroid.x + 180) / 6) + 1
        proj_crs = f"EPSG:{32600 + utm_zone}" if centroid.y > 0 else f"EPSG:{32700 + utm_zone}"

        damage_gdf = damage_gdf.to_crs(proj_crs)
        insured_gdf = insured_gdf.to_crs(proj_crs)

        # ── 面积统计（平方米 → 亩） ──────────────────────────
        SQM_TO_MU = 0.0015
        raw_damage_sqm = damage_gdf.geometry.area.sum()
        insured_sqm = insured_gdf.geometry.area.sum()

        # ── 核心：空间求交 ─────────────────────────────────
        valid_gdf = gpd.overlay(damage_gdf, insured_gdf, how="intersection")

        if valid_gdf.empty:
            return {
                "status": "success",
                "insured_area_mu": round(insured_sqm * 0.0015, 2),
                "raw_damage_area_mu": round(raw_damage_sqm * 0.0015, 2),
                "valid_damage_area_mu": 0.0,
                "excluded_area_mu": round(raw_damage_sqm * 0.0015, 2),
                "damage_ratio": 0.0,
                "clip_log": [
                    "⚠️ 受灾区域与承保红线无交集",
                    f"原始受灾面积: {round(raw_damage_sqm * 0.0015, 2)} 亩 — 全部被剔除",
                    "结论: 受灾区域完全在承保范围之外，建议人工核查地块归属",
                ],
            }

        valid_sqm = valid_gdf.geometry.area.sum()
        excluded_sqm = max(raw_damage_sqm - valid_sqm, 0)
        damage_ratio = valid_sqm / insured_sqm if insured_sqm > 0 else 0.0

        # ── 剔除日志 ───────────────────────────────────────
        clip_log = [
            f"承保面积: {round(insured_sqm * 0.0015, 2)} 亩",
            f"原始受灾面积: {round(raw_damage_sqm * 0.0015, 2)} 亩",
            f"合规受灾面积: {round(valid_sqm * 0.0015, 2)} 亩",
            f"剔除越界面积: {round(excluded_sqm * 0.0015, 2)} 亩",
            f"合规受损比例: {round(damage_ratio * 100, 2)}%",
        ]
        if excluded_sqm > 0.01:
            clip_log.append(f"❗ 已剔除 {round(excluded_sqm * 0.0015, 2)} 亩越界面积")

        return {
            "status": "success",
            "insured_area_mu": round(insured_sqm * 0.0015, 2),
            "raw_damage_area_mu": round(raw_damage_sqm * 0.0015, 2),
            "valid_damage_area_mu": round(valid_sqm * 0.0015, 2),
            "excluded_area_mu": round(excluded_sqm * 0.0015, 2),
            "damage_ratio": round(damage_ratio, 4),
            "clip_log": clip_log,
        }

    except ImportError:
        return {
            "status": "error",
            "error_code": "GEOPANDAS_NOT_INSTALLED",
            "error_message": "geopandas 未安装。",
        }
    except Exception as e:
        logger.error(f"空间求交失败: {e}")
        return {
            "status": "error",
            "error_code": "SPATIAL_CALC_ERROR",
            "error_message": str(e),
        }


if __name__ == "__main__":
    # 测试
    print("=== 格式检测 ===")
    for f in ["boundary.geojson", "plot.shp", "data.gpkg", "map.kml", "doc.pdf"]:
        fmt = _detect_format(f)
        print(f"  {f}: {fmt or '不支持'}")

    print("\n=== 空间求交测试 ===")
    damage = {
        "type": "Polygon",
        "coordinates": [[[113.51, 34.51], [113.59, 34.51], [113.59, 34.59], [113.51, 34.59], [113.51, 34.51]]],
    }
    insured = {
        "type": "Polygon",
        "coordinates": [[[113.50, 34.50], [113.55, 34.50], [113.55, 34.55], [113.50, 34.55], [113.50, 34.50]]],
    }
    result = calculate_valid_claim_area(damage, insured)
    print(json.dumps(result, ensure_ascii=False, indent=2))
