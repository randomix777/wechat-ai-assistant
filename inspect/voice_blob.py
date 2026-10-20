"""语音 blob 解开看有没有文字；顺带列 db_storage 下有没有转写相关的库。"""
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from wechat_ai_assistant.wcdb import WcdbReader
from wechat_ai_assistant.WeChatTools import Tools
from wechat_ai_assistant.paths import default_data_dir

storage = Path(Tools.where_wxid_folder()) / "db_storage"
print("db_storage children:", sorted(p.name for p in storage.iterdir()))
data_dir = default_data_dir()
keys = data_dir / "wcdb_keys.json"

reader = WcdbReader(storage, keys)
ready = reader.prepare(["message/message_0.db"])
try:
    con = sqlite3.connect(str(ready["message/message_0.db"]))
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Msg_%'")]
    blob = None
    for table in tables[:60]:
        row = con.execute(
            f"SELECT message_content FROM {table} WHERE local_type=34 LIMIT 1").fetchone()
        if row and row[0]:
            blob = bytes(row[0])
            print("voice blob from", table, "len:", len(blob))
            break
    con.close()
    if blob:
        import zstandard
        raw = blob
        if raw[:4] == b"\x28\xb5\x2f\xfd":
            raw = zstandard.ZstdDecompressor().decompress(raw)
        print("decompressed len:", len(raw))
        # 找可读文本片段
        texts = re.findall(rb"[\x20-\x7e]{6,}", raw)
        print("ascii runs:", len(texts))
        for t in texts[:20]:
            print("  ", t[:100])
        cjk = re.findall(
            b"(?:[\xe4-\xe9][\x80-\xbf]{2}){2,}", raw)
        print("cjk runs:", len(cjk))
        for t in cjk[:10]:
            try:
                print("  ", t[:90].decode("utf-8"))
            except Exception:
                pass
finally:
    reader.cleanup(ready)
print("done")
