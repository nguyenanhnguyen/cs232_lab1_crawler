"""Xuất thống kê và đặc trưng dữ liệu từ SQLite sau khi thu thập."""

import argparse
import csv
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from statistics import mean, median


def key(collection_id: int, value: str) -> str:
    return hashlib.sha256(f"{collection_id}:{value}".encode("utf-8")).hexdigest()[:16]


def month(value: str | None) -> str:
    return value[:7] if value and re.match(r"^\d{4}-(0[1-9]|1[0-2])", value) else "unknown"


def distribution(values: list[int]) -> dict[str, int]:
    bins = {"0": 0, "1-9": 0, "10-99": 0, "100-999": 0, "1000+": 0}
    for value in values:
        label = "0" if value <= 0 else "1-9" if value < 10 else "10-99" if value < 100 else "100-999" if value < 1000 else "1000+"
        bins[label] += 1
    return bins


def describe(values: list[int]) -> dict[str, float | int]:
    if not values:
        return {"average": 0, "median": 0, "p90": 0, "max": 0}
    ordered = sorted(values)
    return {
        "average": round(mean(values), 2),
        "median": median(ordered),
        "p90": ordered[(len(ordered) * 9 + 9) // 10 - 1],
        "max": ordered[-1],
    }


def write_csv(path: Path, columns: list[str], rows: list[tuple]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def export(db: Path, output: Path) -> dict:
    if not db.is_file():
        raise FileNotFoundError(f"Không thấy database: {db}")
    output.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("BEGIN")
        collections = {row["id"]: row["name"] for row in connection.execute("SELECT id, name FROM collections")}
        posts = list(connection.execute("""
            SELECT collection_id, post_id, created_at, text, like_count, comment_count,
                   image_urls_json, comments_saved, comments_status, permalink
            FROM posts ORDER BY collection_id, id
        """))
        comments = list(connection.execute("""
            SELECT collection_id, post_id, parent_id, depth, text, like_count
            FROM comments ORDER BY collection_id, id
        """))
        runs = [dict(row) for row in connection.execute(
            "SELECT status, COUNT(*) AS count FROM runs GROUP BY status ORDER BY status"
        )]
    finally:
        connection.close()

    post_features = []
    by_month = Counter()
    statuses = Counter()
    collection_counts = Counter()
    likes, reported_comments, text_lengths, images = [], [], [], []
    for post in posts:
        collection_id = post["collection_id"]
        image_count = len(json.loads(post["image_urls_json"] or "[]"))
        text_length = len((post["text"] or "").strip())
        created_month = month(post["created_at"])
        by_month[(collection_id, created_month)] += 1
        collection_counts[collection_id] += 1
        statuses[post["comments_status"] or "unknown"] += 1
        likes.append(post["like_count"])
        reported_comments.append(post["comment_count"])
        text_lengths.append(text_length)
        images.append(image_count)
        post_features.append((key(collection_id, post["post_id"]), collection_id, created_month,
                              post["like_count"], post["comment_count"], post["comments_saved"],
                              text_length, image_count, post["comments_status"], int(bool(post["permalink"]))))

    comment_features = []
    depths = Counter()
    comment_likes, comment_lengths = [], []
    for comment in comments:
        depth = comment["depth"]
        length = len((comment["text"] or "").strip())
        depths[depth] += 1
        comment_likes.append(comment["like_count"])
        comment_lengths.append(length)
        comment_features.append((key(comment["collection_id"], comment["post_id"]), depth,
                                 int(bool(comment["parent_id"])), comment["like_count"], length))

    write_csv(output / "posts_features.csv",
              ["post_key", "collection_id", "created_month", "like_count", "reported_comments",
               "saved_comments", "text_length", "image_count", "comments_status", "has_permalink"], post_features)
    write_csv(output / "comments_features.csv",
              ["post_key", "depth", "is_reply", "like_count", "text_length"], comment_features)
    write_csv(output / "posts_by_month.csv", ["collection_id", "month", "post_count"],
              [(cid, period, count) for (cid, period), count in sorted(by_month.items())])

    summary = {
        "posts": len(posts),
        "comments_saved": len(comments),
        "collections": [{"id": cid, "name": name, "posts": collection_counts[cid]}
                        for cid, name in sorted(collections.items())],
        "crawl_runs_by_status": {row["status"]: row["count"] for row in runs},
        "posts_with_text": sum(value > 0 for value in text_lengths),
        "posts_with_images": sum(value > 0 for value in images),
        "posts_with_permalink": sum(row[-1] for row in post_features),
        "posts_with_reported_comments": sum(value > 0 for value in reported_comments),
        "comment_status": dict(sorted(statuses.items())),
        "comment_depth": {str(depth): count for depth, count in sorted(depths.items())},
        "post_likes": describe(likes),
        "post_likes_bins": distribution(likes),
        "reported_comments_per_post": describe(reported_comments),
        "post_text_length": describe(text_lengths),
        "images_per_post": describe(images),
        "comment_likes": describe(comment_likes),
        "comment_text_length": describe(comment_lengths),
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Xuất dữ liệu EDA từ crawler.db")
    parser.add_argument("--db", type=Path, default=Path("data/crawler.db"), help="Đường dẫn SQLite")
    parser.add_argument("--out", type=Path, default=Path("eda"), help="Thư mục kết quả")
    args = parser.parse_args()
    try:
        result = export(args.db, args.out)
    except (FileNotFoundError, sqlite3.Error) as exc:
        parser.exit(1, f"Lỗi EDA: {exc}\n")
    print(f"Đã xuất {result['posts']:,} bài, {result['comments_saved']:,} bình luận vào {args.out}")
