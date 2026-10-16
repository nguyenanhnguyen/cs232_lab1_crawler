"""Đọc payload và hợp nhất thông tin bài viết theo thứ tự xuất hiện."""

import hashlib
import json
import re
from .data import Post
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit


_BODY_KEYS = ("message", "text", "body", "content")
_TIME_KEYS = ("created_time", "created_at", "timestamp")
_ID_KEYS = ("post_id", "story_id", "id")
_SKIP_PARENTS = {"comments", "comment", "likers", "reactions", "replies"}
_SKIP_KIND = {"comment", "user", "avatar", "sticker", "emoji", "icon"}
_BAD_IMAGE = ("avatar", "emoji", "sticker", "icon", "profile_pic", "sprite")
_INT_RE = re.compile(r"^\d{1,3}(,\d{3})+$|^\d+$")

def parse_json_payloads(raw: bytes | str) -> list[Any]:
    text = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else raw
    text = text.strip()
    if not text:
        return []
    if text.startswith("for (;;);"):
        text = text[len("for (;;);") :].lstrip()
    if text[0] not in "[{":
        return []
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    docs: list[Any] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line[0] not in "[{":
            continue
        try:
            docs.append(json.loads(line))
        except ValueError:
            continue
    return docs

def extract_posts(payload: Any, base_url: str = "") -> list[Post]:
    found: list[Post] = []
    _walk(payload, found, "", base_url)
    return merge_posts(found)

def _first_id(data: dict[str, Any]) -> str:
    for key in ("post_id", "story_id", "id"):
        value = data.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        token = str(value).strip()
        if token:
            return token
    return ""

def cursor_has_next(payload: Any) -> bool | None:
    flags: list[bool] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("has_next_page"), bool):
                flags.append(node["has_next_page"])
            for key in ("next_cursor", "end_cursor", "after"):
                if key in node and node[key] in (None, ""):
                    flags.append(False)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload)
    if not flags:
        return None
    if any(flags):
        return True
    return False

def merge_posts(posts: list[Post]) -> list[Post]:
    """Mỗi ID giữ vị trí đầu tiên; bản trùng bổ sung các trường còn thiếu."""
    merged: dict[str, Post] = {}
    for post in posts:
        previous = merged.get(post.post_id)
        merged[post.post_id] = post if previous is None else _richer(previous, post)
    return list(merged.values())

def _walk(node: Any, found: list[Post], parent_key: str, base_url: str) -> None:
    if isinstance(node, dict):
        if parent_key not in _SKIP_PARENTS and _looks_like_post(node):
            post = _to_post(node, base_url)
            if post is not None:
                found.append(post)
        for key, value in node.items():
            _walk(value, found, key, base_url)
    elif isinstance(node, list):
        for item in node:
            _walk(item, found, parent_key, base_url)

def _looks_like_post(data: dict[str, Any]) -> bool:
    kind = str(data.get("type") or data.get("__typename") or "").lower()
    if kind in _SKIP_KIND:
        return False
    if "feedback" in kind:
        return False
    if "viewer_actor" in data or "actor_provider" in data or "feedback_context" in data:
        return False
    if any(key in data for key in ("display_comments", "comment_rendering_instance", "owning_profile")):
        return False
    has_body = any(isinstance(data.get(key), str) for key in _BODY_KEYS)
    has_author = _author_name(data) != ""
    has_engage = any(key in data for key in ("like_count", "comment_count", "reaction_count", "likes", "comments"))
    if any(key in data for key in ("post_id", "story_id")):
        has_body = any(isinstance(data.get(key), str) and data.get(key).strip() for key in _BODY_KEYS)
        has_author = _author_name(data) != ""
        if has_body or has_author:
            return True
        return False
    has_id = any(key in data and not isinstance(data.get(key), (dict, list)) and str(data.get(key)).strip() for key in _ID_KEYS)
    if has_id and (has_body or has_engage):
        return True
    return bool(has_body and has_author and has_engage)

def _to_post(data: dict[str, Any], base_url: str) -> Post | None:
    if _is_fb_story(data):
        return _fb_post(data, base_url)
    author = _author_name(data)
    created_at, inferred_label = _split_time(_first(data, _TIME_KEYS))
    label = data.get("time_label") or data.get("created_time_text") or inferred_label
    if isinstance(label, str):
        label = label.strip() or None
    else:
        label = inferred_label
    text = _text(data)
    permalink = _permalink(data, base_url)
    post_id, unstable = _identity(data, author, created_at, permalink, text)
    if not post_id:
        return None
    images = _images(data, base_url)
    if not permalink:
        permalink = _fallback_permalink(post_id, base_url)
    raw = {key: value for key, value in data.items() if key != "comments"}
    return Post(
        post_id=post_id,
        author=author,
        created_at=created_at,
        time_label=label if isinstance(label, str) else None,
        like_count=_first_count(data, ("like_count", "reaction_count", "likes")),
        comment_count=_first_count(data, ("comment_count", "comments")),
        text=text,
        image_urls=images,
        permalink=permalink,
        id_unstable=unstable,
        raw=raw,
    )

def _is_fb_story(data: dict[str, Any]) -> bool:
    if data.get("__typename") == "Story":
        return True
    return "post_id" in data and "creation_time" in data and "feedback" in data

def _fb_post(data: dict[str, Any], base_url: str) -> Post | None:
    author = _fb_author(data)
    created_at, _ = _split_time(data.get("creation_time"))
    text = _fb_text(data)
    permalink = _fb_permalink(data, base_url)
    post_id, unstable = _identity(data, author, created_at, permalink, text)
    if not post_id:
        return None
    like_count, comment_count = _fb_counts(data)
    if not permalink:
        permalink = _fallback_permalink(post_id, base_url)
    return Post(
        post_id=post_id,
        author=author,
        created_at=created_at,
        time_label=None,
        like_count=like_count,
        comment_count=comment_count,
        text=text,
        image_urls=_fb_images(data, base_url),
        permalink=permalink,
        id_unstable=unstable,
        raw=None,
    )

def _fb_author(data: dict[str, Any]) -> str:
    for key in ("actor", "author", "from", "owner", "user", "page"):
        value = data.get(key)
        if isinstance(value, dict):
            name = value.get("name") or value.get("full_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
        if isinstance(value, str) and value.strip():
            return value.strip()

    actors = data.get("actors")
    if isinstance(actors, list):
        for actor in actors:
            if isinstance(actor, dict):
                name = actor.get("name") or actor.get("full_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()

    for container_key in ("feedback", "story", "post", "node"):
        container = data.get(container_key)
        if isinstance(container, dict):
            owner = container.get("owning_profile") or container.get("profile") or container.get("from")
            if isinstance(owner, dict):
                name = owner.get("name") or owner.get("full_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
            for key in ("actor", "author"):
                value = container.get(key)
                if isinstance(value, dict):
                    name = value.get("name") or value.get("full_name")
                    if isinstance(name, str) and name.strip():
                        return name.strip()

    def walk(node: Any) -> str | None:
        if isinstance(node, dict):
            for key in ("name", "full_name"):
                v = node.get(key)
                if isinstance(v, str) and v.strip() and node.get("__typename") in (None, "User", "Page", "Profile"):
                    return v.strip()
            for k in ("actor", "author", "from", "owner"):
                if k in node:
                    r = walk(node[k])
                    if r:
                        return r
            for v in node.values():
                r = walk(v)
                if r:
                    return r
        elif isinstance(node, list):
            for item in node:
                r = walk(item)
                if r:
                    return r
        return None

    w = walk(data)
    return w or ""

def _fb_text(data: dict[str, Any]) -> str:
    candidates: list[str] = []
    for key in ("message", "description", "summary"):
        node = data.get(key)
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and text.strip():
                candidates.append(text.strip())
        elif isinstance(node, str) and node.strip():
            candidates.append(node.strip())
    if candidates:
        return max(candidates, key=len)
    best = ""

    def walk(node: Any) -> None:
        nonlocal best
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str) and isinstance(node.get("ranges"), list):
                cleaned = text.strip()
                if len(cleaned) > len(best):
                    best = cleaned
            for key, value in node.items():
                if key in ("attached_story", "comments", "display_comments", "replies", "feedback"):
                    continue
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return best

def _fb_counts(data: dict[str, Any]) -> tuple[int, int]:
    like = 0
    comment_count = 0

    def walk(node: Any) -> None:
        nonlocal like, comment_count
        if isinstance(node, dict):
            reaction = node.get("reaction_count")
            if not like and isinstance(reaction, dict) and isinstance(reaction.get("count"), int):
                like = int(reaction["count"])
            comments = node.get("comments")
            if not comment_count and isinstance(comments, dict) and isinstance(comments.get("total_count"), int):
                comment_count = int(comments["total_count"])
            for key, value in node.items():
                if key == "attached_story":
                    continue
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return like, comment_count

def _fb_images(data: dict[str, Any], base_url: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()

    def add(url: Any) -> None:
        if not isinstance(url, str) or not url.strip():
            return
        absolute = _abs(url.strip(), base_url)
        if not absolute or not _good_image(absolute) or absolute in seen:
            return
        seen.add(absolute)
        found.append(absolute)

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            viewer = node.get("viewer_image")
            if isinstance(viewer, dict):
                add(viewer.get("uri"))
            else:
                image = node.get("image")
                if (
                    node.get("__typename") == "Photo"
                    and isinstance(image, dict)
                    and isinstance(image.get("uri"), str)
                ):
                    add(image.get("uri"))
            for key, value in node.items():
                if key == "attached_story":
                    continue
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found

def _fb_permalink(data: dict[str, Any], base_url: str) -> str | None:
    for key in (
        "permalink_url",
        "www_url",
        "url",
        "story_url",
        "story_permalink_url",
        "post_permalink_url",
        "share_url",
        "share_url_v2",
        "link",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and not _looks_like_image(value):
            url = _abs(value.strip(), base_url)
            if "facebook.com" in url:
                return url
    node = data.get("url")
    if isinstance(node, dict):
        return _fb_permalink(node, base_url)
    actors = data.get("actors")
    if isinstance(actors, list) and actors and isinstance(actors[0], dict):
        url = actors[0].get("url")
        if isinstance(url, str) and url.strip():
            return _abs(url.strip(), base_url)
    return None

def _fallback_permalink(post_id: str, base_url: str) -> str | None:
    if not post_id or not base_url:
        return None
    parsed = urlsplit(base_url)
    if not parsed.netloc:
        return None
    path = parsed.path.rstrip("/")
    if "/groups/" in path:
        return f"{parsed.scheme}://{parsed.netloc}{path}/permalink/{post_id}/"
    if path and path not in ("/", ""):
        return f"{parsed.scheme}://{parsed.netloc}{path}/posts/{post_id}"
    return None

def _identity(
    data: dict[str, Any],
    author: str,
    created_at: str | None,
    permalink: str | None,
    text: str,
) -> tuple[str | None, bool]:
    for key in _ID_KEYS:
        value = data.get(key)
        if value is None or isinstance(value, (dict, list)):
            continue
        token = str(value).strip()
        if token:
            return token, False
    if not (author or created_at or permalink or text):
        return None, False
    raw = "|".join([author, created_at or "", permalink or "", text[:80]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20], True

def _author_name(data: dict[str, Any]) -> str:
    for key in ("author", "from", "actor", "page", "user", "owner"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            name = value.get("name") or value.get("full_name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    actors = data.get("actors")
    if isinstance(actors, list):
        for actor in actors:
            if isinstance(actor, dict):
                name = actor.get("name") or actor.get("full_name")
                if isinstance(name, str) and name.strip():
                    return name.strip()
    return ""

def _text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for key in _BODY_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            chunks.append(value.strip())
    if not chunks:
        return ""
    return max(chunks, key=len)

def _permalink(data: dict[str, Any], base_url: str) -> str | None:
    for key in (
        "permalink",
        "permalink_url",
        "post_url",
        "post_permalink_url",
        "story_url",
        "story_permalink_url",
        "share_url",
        "share_url_v2",
        "url",
        "link",
    ):
        value = data.get(key)
        if isinstance(value, str) and value.strip() and not _is_image_key(key, value):
            if key == "url" and _looks_like_image(value):
                continue
            return _abs(value.strip(), base_url)
    return None

def _images(data: dict[str, Any], base_url: str) -> list[str]:
    found: list[str] = []
    raw_list = data.get("image_urls")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict):
                found.extend(_image_from_obj(item, 0))
    for key in ("images", "media", "attachments", "subattachments"):
        found.extend(_image_from_value(data.get(key), 0))
    if isinstance(data.get("full_picture"), str):
        found.append(data["full_picture"])
    elif isinstance(data.get("picture"), str):
        found.append(data["picture"])
    clean: list[str] = []
    seen: set[str] = set()
    for item in found:
        absolute = _abs(item, base_url)
        if not absolute or not _good_image(absolute):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        clean.append(absolute)
    return clean

def _image_from_value(value: Any, depth: int) -> list[str]:
    if depth > 5 or value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        found: list[str] = []
        sized: list[tuple[int, str]] = []
        plain: list[str] = []
        for item in value:
            if isinstance(item, str):
                plain.append(item)
            elif isinstance(item, dict):
                url = item.get("url") or item.get("src") or item.get("uri") or item.get("source")
                if isinstance(url, str) and "width" in item and not any(k in item for k in ("attachments", "subattachments", "media")):
                    sized.append((int(item.get("width") or 0), url))
                else:
                    found.extend(_image_from_obj(item, depth + 1))
        if sized:
            sized.sort()
            found.append(sized[-1][1])
        found.extend(plain)
        return found
    if isinstance(value, dict):
        return _image_from_obj(value, depth + 1)
    return []

def _image_from_obj(obj: dict[str, Any], depth: int) -> list[str]:
    if depth > 5:
        return []
    kind = str(obj.get("type") or obj.get("media_type") or "").lower()
    if kind in _SKIP_KIND or kind in {"video"}:
        return []
    found: list[str] = []
    for key in ("url", "uri", "src", "source"):
        value = obj.get(key)
        if isinstance(value, str):
            found.append(value)
    for key in ("image", "media", "images", "subattachments", "data"):
        if key in obj:
            found.extend(_image_from_value(obj.get(key), depth + 1))
    return found

def acceptable_image(url: str) -> bool:
    return _good_image(url)

def _good_image(url: str) -> bool:
    lowered = url.lower()
    return not any(token in lowered for token in _BAD_IMAGE)

def _looks_like_image(url: str) -> bool:
    path = url.lower().split("?", 1)[0]
    return path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"))

def _is_image_key(key: str, value: str) -> bool:
    return key == "url" and _looks_like_image(value)

def _abs(url: str, base_url: str) -> str:
    if not url:
        return ""
    if url.startswith("data:"):
        return url
    return urljoin(base_url, url)

def _first(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None

def _split_time(value: Any) -> tuple[str | None, str | None]:
    if value is None:
        return None, None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000
        try:
            moment = datetime.fromtimestamp(stamp, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None, str(value)
        return moment.strftime("%Y-%m-%dT%H:%M:%SZ"), None
    if isinstance(value, str):
        text = value.strip()
        if _looks_iso(text):
            return text, None
        return None, text
    return None, None

def _looks_iso(value: str) -> bool:
    candidate = value.replace("Z", "+00:00")
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        return False
    return True

def parse_count(value: Any) -> int:
    parsed = _as_count(value)
    if parsed is not None:
        return parsed
    if isinstance(value, str):
        match = re.search(r"\d[\d,]*", value)
        if match:
            return int(match.group(0).replace(",", ""))
    return 0

def _first_count(data: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        if key not in data:
            continue
        parsed = _as_count(data.get(key))
        if parsed is not None:
            return parsed
    return 0

def _as_count(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if _INT_RE.fullmatch(text):
            return int(text.replace(",", ""))
        return None
    if isinstance(value, dict):
        if "count" in value:
            return _as_count(value.get("count"))
        summary = value.get("summary")
        if isinstance(summary, dict) and "total_count" in summary:
            return _as_count(summary.get("total_count"))
    return None

def _richer(left: Post, right: Post) -> Post:
    fields = {key: getattr(left, key) or getattr(right, key)
              for key in ("author", "created_at", "time_label", "permalink", "raw")}
    return Post(
        post_id=left.post_id,
        text=max((left.text, right.text), key=len),
        like_count=max(left.like_count, right.like_count),
        comment_count=max(left.comment_count, right.comment_count),
        image_urls=list(dict.fromkeys([*left.image_urls, *right.image_urls])),
        id_unstable=left.id_unstable and right.id_unstable,
        **fields,
    )
