"""
.env 파일 읽기/쓰기 유틸리티.
python-dotenv는 파일 쓰기를 지원하지 않으므로 직접 파싱한다.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Dict

REPLACE_ATTEMPTS = 5
REPLACE_RETRY_SECONDS = 0.2


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
    # Windows는 편집기·백신이 파일을 잠깐 쥐고 있으면 교체를 거부한다(2026-11-13 IDE 저장과 겹침) —
    # 잠시 뒤 다시 시도하고, 끝까지 안 되면 원래 예외를 그대로 올린다
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)
