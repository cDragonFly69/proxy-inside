"""users.py — 多用户令牌表，两个代理进程共用。

users.json 结构（面板写入，代理只读）：

    {
      "users": [
        {"name": "alice", "token": "tok-xxxx", "enabled": true},
        {"name": "bob",   "token": "tok-yyyy", "enabled": true}
      ]
    }

按 mtime 热加载：面板增删用户后不用重启代理进程。
"""

import base64
import json
import os

_cache = {"path": None, "mtime": 0.0, "map": {}}


def load(path):
    """返回 {token: name}。文件不存在或坏掉时返回上次成功的映射。"""
    if not path:
        return {}
    try:
        st = os.stat(path)
    except OSError:
        return {}
    if _cache["path"] == path and _cache["mtime"] == st.st_mtime:
        return _cache["map"]
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _cache["map"] if _cache["path"] == path else {}
    table = {}
    for u in data.get("users") or []:
        if not isinstance(u, dict):
            continue
        if not u.get("enabled", True):
            continue
        tok = u.get("token")
        if not tok:
            continue
        table[str(tok)] = str(u.get("name") or "user")
    _cache.update(path=path, mtime=st.st_mtime, map=table)
    return table


def pick(auth_header, token, users_path):
    """从认证头解析出用户名；失败返回 None。

    支持两种形态：
      Proxy-Authorization: Bearer <token>
      Proxy-Authorization: Basic base64(<任意用户名>:<token>)
    SOCKS5 那边自己解析 RFC1929 的 user/pass，不经过这里。
    """
    if not auth_header:
        return None
    low = auth_header.lower()
    if low.startswith("bearer "):
        cand = auth_header[7:].strip()
    elif low.startswith("basic "):
        try:
            raw = base64.b64decode(auth_header[6:].strip()).decode("utf-8", "replace")
        except Exception:
            return None
        cand = raw.split(":", 1)[1] if ":" in raw else ""
    else:
        return None
    if not cand:
        return None
    if token and cand == token:
        return "default"
    table = load(users_path)
    if cand in table:
        return table[cand]
    # 历史行为：令牌大小写不敏感
    low_cand = cand.lower()
    if token and low_cand == token.lower():
        return "default"
    for tok, name in table.items():
        if low_cand == tok.lower():
            return name
    return None


def check_pair(user, password, token, users_path):
    """SOCKS5 RFC1929 用：user 忽略，password 当令牌。"""
    if not password:
        return None
    if token and password == token:
        return user or "default"
    table = load(users_path)
    if password in table:
        return table[password]
    low = password.lower()
    if token and low == token.lower():
        return user or "default"
    for tok, name in table.items():
        if low == tok.lower():
            return name
    return None


def read_all(path):
    """面板用：读出完整用户表（含 disabled），坏文件返回空表。"""
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    out = []
    for u in data.get("users") or []:
        if isinstance(u, dict):
            out.append({
                "name": str(u.get("name") or "user"),
                "token": str(u.get("token") or ""),
                "enabled": bool(u.get("enabled", True)),
            })
    return out
