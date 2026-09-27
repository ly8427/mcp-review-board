#!/usr/bin/env bash
# backup-db.sh — thread #31 批C②:活库在线备份(参数化,迁库时只改路径变量)。
# 在 WSL 侧执行(与服务属主同侧):sqlite3 .backup 走 SQLite 在线备份 API,
# 一致快照、不停服、不经 Windows 侧直开(AGENTS.md 2026-09-27「活库禁直读」)。
# 用法(WSL 内): bash /mnt/c/Users/liu/ZCodeProject/mcp-review-board/configs/backup-db.sh
#
# ── 迁库 runbook(thread #31 C③,延期项:作者拍板后单独开帖+计划停机窗口)──
# 1. wsl: systemctl --user stop review-board.service
# 2. wsl: SRC_DB=<旧库> MIGRATE=1 bash configs/backup-db.sh   # .backup 一致拷贝
# 3. 改 systemd unit 的 REVIEWBOARD_DB/路径 → 新库位置
# 4. wsl: systemctl --user start review-board.service
# 5. **校验 board_id 未变**:curl -s "http://localhost:8765/attention?author=probe" | grep -o 7680ea88… 必须同值
#    ——新建库=新 board_id,所有 pin 了 EXPECTED_BOARD_ID 的 watcher 立即 exit 4 硬停(codex #334 F4)。
# 6. 恢复 host 轮询 cron。回滚=指回旧路径,同样跑第 5 步校验;board_id 变了就硬停报告。
set -euo pipefail
SRC_DB="${SRC_DB:-/mnt/c/Users/liu/ZCodeProject/mcp-review-board/data/reviewboard.db}"
DEST_DIR="${DEST_DIR:-$(dirname "$SRC_DB")/backups}"
STAMP="$(date +%Y%m%d-%H%M%S)"
mkdir -p "$DEST_DIR"
sqlite3 "$SRC_DB" ".backup '$DEST_DIR/reviewboard-$STAMP.db'"
echo "backup ok: $DEST_DIR/reviewboard-$STAMP.db"
