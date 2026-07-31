#!/bin/bash
# 构建固件并发布到 OTA 目录。设备开机/定期会拉 /fw/version 比对自更新。
# 用法:改完 firmware,先把 src/config.h 的 FW_VERSION +1,再跑本脚本。
set -e
# 相对脚本位置定位仓库根(deploy/ 的上一级)
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FW="$REPO/firmware"
OUT="$REPO/fw"                       # OTA 产物目录(git 忽略),thumbserver 的 /fw 从这里取
# macOS 上 pip --user 装的 pio 常在此路径;按需调整/删除
export PATH="$HOME/Library/Python/3.9/bin:$PATH"

VER=$(grep -E '#define[[:space:]]+FW_VERSION' "$FW/src/config.h" | awk '{print $3}')
echo "==> 构建固件 v$VER"
cd "$FW"
pio run
mkdir -p "$OUT"
cp .pio/build/PaperS3/firmware.bin "$OUT/current.bin"
echo "$VER" > "$OUT/version.txt"
echo "==> 已发布 v$VER  ($(wc -c < "$OUT/current.bin") 字节)  → $OUT"
echo "    设备下次开机/定期检查时将自更新到 v$VER"
