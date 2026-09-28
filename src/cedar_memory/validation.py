"""在进入数据库或模型回调前验证输入，不把非法原文拼入异常。"""

import math


def text(value: str, name: str, maximum: int, *, identifier: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} 必须是非空字符串，且不超过 {maximum} 字符")
    if any(ord(c) < 32 and c not in "\n\t" for c in value):
        raise ValueError(f"{name} 包含不支持的控制字符")
    if any(0xD800 <= ord(c) <= 0xDFFF for c in value):
        raise ValueError(f"{name} 包含无效的 Unicode 字符")
    if identifier and (value != value.strip() or "\n" in value or "\t" in value):
        raise ValueError(f"{name} 不能包含首尾空白或换行制表符")
    return value


def integer(value: int, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def timeout(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout 必须是正数")
    if not math.isfinite(value) or not 0 < value <= 300:
        raise ValueError("timeout 必须大于 0 且不超过 300 秒")
    return float(value)
