"""采集器公共工具:从结果文本里提取存在的图片文件。"""
from __future__ import annotations

import glob
import hashlib
import os
import re

IMG_EXT = ("png", "jpg", "jpeg", "webp", "bmp", "gif")
# 匹配路径样式的 token(含 * 通配、~、相对/绝对),以图片扩展名结尾
_PAT = re.compile(r"[~\w./\-*]+\.(?:" + "|".join(IMG_EXT) + r")", re.IGNORECASE)

MAX_IMAGES = 4

_MD = re.compile(r"(\*\*|__|`+|~~)")
_MD_HEAD = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)

# 通知端不显示图片,把图片/链接/URL 从正文里剥掉,腾出空间给真正的文字
_MD_IMG = re.compile(r"!\[[^\]]*\]\([^)]*\)")                 # ![alt](path/url)
_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")               # [文字](url) → 文字
_BARE_IMG = re.compile(                                       # 裸图片路径/文件名(仅 ASCII 路径字符,
    r"[A-Za-z0-9./~*_-]+\.(?:" + "|".join(IMG_EXT) + r")\b",   # 避免吃掉紧邻的中文,如「张图:a.png」)
    re.IGNORECASE)
_URL = re.compile(r"https?://\S+")                            # 裸 URL
_WS = re.compile(r"[ \t]{2,}")                                # 多余空格
_BLANK = re.compile(r"\n{3,}")                                # 多余空行


def clean_text(s: str) -> str:
    """清洗正文供墨水屏显示:去 markdown 强调符 + 过滤图片/链接/URL + 压缩空白。"""
    if not s:
        return s
    s = _MD_IMG.sub("", s)          # 先去 markdown 图片(整体删)
    s = _MD_LINK.sub(r"\1", s)      # markdown 链接保留文字、去 URL
    s = _URL.sub("", s)             # 裸 URL 删掉
    s = _BARE_IMG.sub("", s)        # 裸图片路径/文件名删掉
    s = _MD_HEAD.sub("", s)         # 去标题 #
    s = _MD.sub("", s)              # 去强调符 ** __ ` ~~
    s = _WS.sub(" ", s)             # 压缩连续空格
    s = _BLANK.sub("\n\n", s)       # 压缩连续空行
    return s.strip()


def img_id(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def extract_images(text: str, cwd: str | None):
    """从文本里找出真实存在的图片文件,返回 [{id,name,path}]。支持 glob 展开。"""
    if not text:
        return []
    found = []
    seen = set()
    for m in _PAT.findall(text):
        token = m
        cands = []
        # 绝对/家目录
        for base in ([token] if os.path.isabs(token) or token.startswith("~")
                     else ([os.path.join(cwd, token)] if cwd else []) + [token]):
            base = os.path.expanduser(base)
            if any(c in base for c in "*?[]"):
                cands.extend(sorted(glob.glob(base)))
            else:
                cands.append(base)
        for p in cands:
            rp = os.path.realpath(p)
            if rp in seen:
                continue
            if os.path.isfile(rp) and rp.split(".")[-1].lower() in IMG_EXT:
                seen.add(rp)
                found.append({"id": img_id(rp), "name": os.path.basename(rp), "path": rp})
                if len(found) >= MAX_IMAGES:
                    return found
    return found
