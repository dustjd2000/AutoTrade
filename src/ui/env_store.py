"""
.env 파일 읽기/쓰기 유틸리티.
python-dotenv는 파일 쓰기를 지원하지 않으므로 직접 파싱한다.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict


def load_env(path: Path) -> Dict[str, str]:
    result: Dict[str, str] = {}
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip()
    return result


def save_env(path: Path, values: Dict[str, str]) -> None:
    existing = load_env(path)
    existing.update(values)

    lines = []
    if path.exists():
        raw_lines = path.read_text(encoding="utf-8").splitlines()
        written_keys: set = set()
        for line in raw_lines:
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                lines.append(line)
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in values:
                    lines.append(f"{key}={values[key]}")
                    written_keys.add(key)
                else:
                    lines.append(line)
        # 기존에 없던 새 키 추가
        for key, val in values.items():
            if key not in written_keys:
                lines.append(f"{key}={val}")
    else:
        for key, val in values.items():
            lines.append(f"{key}={val}")

    # 자격증명이 든 파일이라 쓰다 죽어도 비지 않게 임시 파일에 쓴 뒤 통째로 바꾼다
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)
