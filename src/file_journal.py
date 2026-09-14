"""
文件修改台账（File Journal）

记录一次任务运行期间对文件系统的改动，任务被中止
（超过最大步数 / 连续失败）时把工作区回滚到任务开始前的状态：

- 任务前已存在的文件：恢复原始内容（write_file 写入前的备份）
- 任务期间新出现的文件（写了一半的网页、下载到一半的文件、
  后台 Blender 刚生成的 .blend 等）：直接删除

工作方式：
1. 引擎在任务开始时调用 begin()：清空台账 + 对工作区拍文件清单快照
2. write_file 等工具在动笔前调用 record()：只备份该文件的最初版本
3. 任务正常结束 → commit() 丢弃备份；任务中止 → rollback() 整体回滚

注意：台账是进程级单例，本项目为单用户本地工具，不处理并发任务。
"""

import os
import shutil
import tempfile
import threading

_lock = threading.RLock()
_records = {}      # 绝对路径 -> None（原本不存在）| 备份文件路径
_snapshot = set()  # begin() 时工作区已有文件的绝对路径集合
_backup_dir = None
_workspace = None

# 快照与清理时跳过的目录名（版本控制、缓存、依赖目录不回滚）
# discussions/ 是多模型讨论的记录，不是 Agent 的工作产物：rollback() 会无差别
# 删除「快照之外的新文件」，不豁免的话某次任务中止就可能顺手把讨论记录扫掉。
_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode",
              "discussions"}
# 跳过的文件名（服务日志等持续变化的文件，永不清理）
_SKIP_FILES = {"blender_live.log"}


def begin(workspace: str) -> None:
    """开始一次任务：重置台账，并对工作区拍快照"""
    global _backup_dir, _workspace
    with _lock:
        _records.clear()
        _snapshot.clear()
        _workspace = os.path.abspath(workspace)
        _backup_dir = tempfile.mkdtemp(prefix="agent_journal_")
        for root, dirs, files in os.walk(_workspace):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                if name not in _SKIP_FILES:
                    _snapshot.add(os.path.join(root, name))


def record(path: str) -> None:
    """
    写入前备份：同一文件在一次任务里只备份最初版本。
    原本不存在的文件记为 None，回滚时删除。
    """
    if not _backup_dir:
        return
    ap = os.path.abspath(path)
    with _lock:
        if ap in _records:
            return
        if os.path.exists(ap):
            try:
                backup = os.path.join(_backup_dir, "f%d" % len(_records))
                shutil.copy2(ap, backup)
                _records[ap] = backup
            except OSError:
                pass  # 备份失败宁可不回滚，也不丢数据
        else:
            _records[ap] = None


def rollback() -> tuple:
    """
    回滚本次任务的全部文件改动。
    返回 (恢复的文件列表, 删除的文件列表)，供引擎拼进中止提示。
    """
    restored, deleted = [], []
    with _lock:
        # 1) 台账记录的文件：恢复原内容，或删除本次新建的文件
        for ap, backup in _records.items():
            try:
                if backup is None:
                    if os.path.exists(ap):
                        os.remove(ap)
                        deleted.append(ap)
                else:
                    os.makedirs(os.path.dirname(ap) or ".", exist_ok=True)
                    shutil.copy2(backup, ap)
                    restored.append(ap)
            except OSError:
                pass

        # 2) 快照之外的新文件：清理（下载一半、子进程生成的半成品等）
        if _workspace:
            for root, dirs, files in os.walk(_workspace):
                dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
                for name in files:
                    if name in _SKIP_FILES:
                        continue
                    fp = os.path.join(root, name)
                    if fp not in _snapshot and fp not in _records:
                        try:
                            os.remove(fp)
                            deleted.append(fp)
                        except OSError:
                            pass

        _cleanup_locked()
    return restored, deleted


def commit() -> None:
    """任务正常完成：丢弃备份，保留所有文件"""
    with _lock:
        _cleanup_locked()


def _cleanup_locked() -> None:
    """清空台账并删除备份目录（须持锁调用）"""
    global _backup_dir, _workspace
    if _backup_dir and os.path.isdir(_backup_dir):
        shutil.rmtree(_backup_dir, ignore_errors=True)
    _backup_dir = None
    _workspace = None
    _records.clear()
    _snapshot.clear()
