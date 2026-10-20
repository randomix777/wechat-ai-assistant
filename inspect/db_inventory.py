"""解密库全盘点：每个库有哪些表、多少行、关键列。只读结构，不打印内容。"""
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wechat_ai_assistant.wcdb import WcdbReader
from wechat_ai_assistant.WeChatTools import Tools
from wechat_ai_assistant.paths import default_data_dir

storage = Path(Tools.where_wxid_folder()) / "db_storage"
keys = default_data_dir() / "wcdb_keys.json"

reader = WcdbReader(storage, keys)
all_rels = []
for sub in ("contact", "session", "message", "general", "emoticon", "favorite",
            "sns", "solitaire", "chatbot", "bizchat", "third_app_icon"):
    d = storage / sub
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.suffix == ".db":
                all_rels.append(f"{sub}/{p.name}")
print("dbs:", len(all_rels))

import json
keymap = json.loads(keys.read_text(encoding="utf-8"))
norm = {k.replace("\\", "/"): v for k, v in keymap.items() if k != "_db_dir"}
keyed = [r for r in all_rels if r in norm]
print("keyed:", len(keyed), keyed)

# prepare 要正斜杠（内部查钥匙两种都认），返回的 ready 也是正斜杠键。
ready = reader.prepare([r for r in keyed if r not in
                        ("contact/contact.db", "session/session.db")])
try:
    for rel in keyed:
        con = sqlite3.connect(str(ready[rel]))
        try:
            tables = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
            msg_tables = [t for t in tables if t.startswith("Msg_")]
            print(f"== {rel}: {len(tables)} tables (Msg_*{len(msg_tables)})")
            for table in tables:
                if table.startswith("Msg_"):
                    continue
                try:
                    n = con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                except Exception as e:
                    print(f"   {table}: count failed {e}")
                    continue
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({table})")]
                print(f"   {table} rows={n}")
                print(f"      cols={cols}")
            if msg_tables:
                total = 0
                for table in msg_tables:
                    try:
                        total += con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    except Exception:
                        pass
                print(f"   Msg_* total rows={total}")
                cols = [r[1] for r in con.execute(f"PRAGMA table_info({msg_tables[0]})")]
                print(f"   Msg columns={cols}")
        finally:
            con.close()
finally:
    reader.cleanup(ready)
print("done")
