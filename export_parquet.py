import argparse
import sqlite3
from contextlib import closing
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


BASE = Path(__file__).resolve().parent
REPO = BASE if BASE.name == "cs232_lab1" else BASE / "cs232_lab1"
DB = REPO / "data" / "crawler.db"
if not DB.is_file():
    DB = REPO.parent / "data" / "crawler.db"


def table_schema(conn, table):
    types = {"INTEGER": pa.int64(), "TEXT": pa.string()}
    return pa.schema([
        pa.field(row["name"], types[row["type"].upper()])
        for row in conn.execute(f'PRAGMA table_info("{table}")')
    ])


def export_parquet(db_path, out_path):
    with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN")
        post_schema = table_schema(conn, "posts")
        comment_schema = table_schema(conn, "comments")
        posts = {}
        for row in conn.execute("SELECT * FROM posts ORDER BY collection_id, post_id"):
            post = dict(row)
            key = (post["collection_id"], post["post_id"])
            if key in posts:
                raise ValueError(f"Trùng khóa bài viết: {key}")
            post["comments"] = []
            posts[key] = post

        for row in conn.execute("SELECT * FROM comments ORDER BY collection_id, post_id, depth, id"):
            comment = dict(row)
            key = (comment["collection_id"], comment["post_id"])
            if key not in posts:
                raise ValueError(f"Comment không có bài viết tương ứng: {comment['comment_id']}")
            posts[key]["comments"].append(comment)

    for post in posts.values():
        post["exported_comment_count"] = len(post["comments"])
    schema = post_schema.append(pa.field("comments", pa.list_(pa.struct(comment_schema))))
    schema = schema.append(pa.field("exported_comment_count", pa.int64()))
    table = pa.Table.from_pylist(list(posts.values()), schema=schema)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, out_path, compression="zstd")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--out", type=Path, default=REPO / "data" / "facebook_data.parquet")
    args = parser.parse_args()
    export_parquet(args.db, args.out)
    print(args.out.name)
    print("DONE")


if __name__ == "__main__":
    main()
