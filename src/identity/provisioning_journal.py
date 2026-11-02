"""Windows 可恢复成功日志及原子权威 JSONL 快照。"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


class ProvisioningJournal:
    """每个成功结果独立事务落盘，输出损坏或进程退出不重复分配身份。"""

    def __init__(self, path):
        """取得独占文件锁并打开逐条同步提交的恢复日志。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = self.path.with_suffix(self.path.suffix + ".lock").open("a+b")
        self.lock.seek(0, 2)
        if self.lock.tell() == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            if sys.platform == "win32":
                import msvcrt

                msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.connection = sqlite3.connect(self.path)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute("""CREATE TABLE IF NOT EXISTS provisioned (
                discord_user_id TEXT PRIMARY KEY,user_id TEXT NOT NULL UNIQUE,operation_id TEXT NOT NULL,
                record TEXT,verified_run TEXT)""")
            self.connection.commit()
        except Exception:
            if hasattr(self, "connection"):
                self.connection.close()
            self.lock.close()
            raise

    def record_ensured(self, discord_id, user_id, operation_id):
        """创建 RPC 一旦成功立即提交，资料读取失败也保留恢复依据。"""
        previous = self.connection.execute(
            "SELECT user_id FROM provisioned WHERE discord_user_id=?", (discord_id,)
        ).fetchone()
        if previous and previous[0] != user_id:
            raise ValueError("IDENTITY_CONFLICT")
        with self.connection:
            self.connection.execute(
                """INSERT INTO provisioned(discord_user_id,user_id,operation_id) VALUES(?,?,?)
                ON CONFLICT(discord_user_id) DO UPDATE SET operation_id=excluded.operation_id""",
                (discord_id, user_id, operation_id),
            )

    def record_complete(self, record, operation_id, run_id):
        """权威绑定版本与原生资料核对完成后，原子标记本轮成功。"""
        self.record_ensured(record["discord_user_id"], record["user_id"], operation_id)
        # 只保存可导入字段，不接收 token、密码或原始来源中的其他字段。
        clean = {
            key: record[key]
            for key in (
                "discord_user_id",
                "user_id",
                "version",
                "username",
                "display_name",
                "avatar_url",
            )
        }
        with self.connection:
            self.connection.execute(
                "UPDATE provisioned SET record=?,verified_run=? WHERE discord_user_id=?",
                (
                    json.dumps(clean, ensure_ascii=False),
                    run_id,
                    record["discord_user_id"],
                ),
            )

    def export(self, output, run_id):
        """临时文件完全同步后替换输出，写入失败时保留旧快照。"""
        output = Path(output)
        if output.resolve() == self.path.resolve():
            raise ValueError("OUTPUT_IS_JOURNAL")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        count = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=output.parent,
                prefix="." + output.name + ".",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                for (record,) in self.connection.execute(
                    "SELECT record FROM provisioned WHERE verified_run=? ORDER BY discord_user_id",
                    (run_id,),
                ):
                    stream.write(record + "\n")
                    count += 1
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, output)
            return count
        finally:
            if temporary and temporary.exists():
                temporary.unlink()

    def close(self):
        """关闭日志及操作系统锁，崩溃后的锁由 Windows 自动释放。"""
        try:
            self.connection.close()
        finally:
            self.lock.close()
