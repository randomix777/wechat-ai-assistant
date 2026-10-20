"""查解密库里有没有语音转文字：列消息库全表结构 + 抽语音行看内容形状。

只读：复用 WcdbReader.prepare（拷贝解密，用完即删），不碰原库。
语音行内容只打印前 200 字看结构，不扩散隐私。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wechat_ai_assistant.wcdb import WcdbReader
from wechat_ai_assistant.WeChatTools import Tools
from wechat_ai_assistant.paths import default_data_dir

folder = Tools.where_wxid_folder()
print("wxid folder:", folder)
storage = Path(folder) / "db_storage"
data_dir = default_data_dir()
keys = None
for cand in (data_dir / "wcdb_keys.json",):
    if cand.is_file():
        keys = cand
        break
print("keys:", keys)
if keys is None:
    print("no keys file; expected", data_dir / "wcdb_keys.json")
    raise SystemExit(1)

reader = WcdbReader(storage, keys)
shards = reader.message_shards()
print("shards:", shards)
ready = reader.prepare(shards)
try:
    for rel, db_path in ready.items():
        if not rel.startswith("message/"):
            continue
        con = sqlite3.connect(str(db_path))
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            print(f"== {rel}: {len(tables)} tables")
            for table in tables:
                if table.startswith("Msg_") or table.startswith("sqlite_"):
                    continue
                print("   non-msg table:", table)
            # 找一张 Msg 表看全列
            msg_tables = [t for t in tables if t.startswith("Msg_")]
            if msg_tables:
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({msg_tables[0]})")]
                print(f"   {msg_tables[0]} columns ({len(cols)}):")
                print("   ", cols)
            # 语音行抽样（全分片只看形状）
            n_voice = 0
            for table in msg_tables[:40]:
                try:
                    cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
                    if "local_type" not in cols or "message_content" not in cols:
                        continue
                    rows = con.execute(
                        f"SELECT local_type, substr(message_content, 1, 200) FROM {table} "
                        "WHERE local_type IN (34, 35, 36) LIMIT 3").fetchall()
                    for lt, content in rows:
                        n_voice += 1
                        print(f"   voice local_type={lt} content[:200]={content!r}")
                except Exception as e:
                    print("   skip", table, e)
            print(f"   voice rows sampled in {rel}: {n_voice}")
        finally:
            con.close()
finally:
    reader.cleanup(ready)
print("done")
