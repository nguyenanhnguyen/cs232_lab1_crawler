"""Giao diện HTTP, thông báo tiến độ và các lệnh chạy ứng dụng."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "cs232_lab1"

import asyncio
import contextlib
import json
import typer
import uvicorn
from .crawler import PlaywrightSource, crawl_comments, crawl_feed
from .data import AppState, Store
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field


Opener = Callable[[str, bool, float, bool], Awaitable[None]]
CommentOpener = Callable[[int, int, "int | None"], Awaitable[None]]

class OpenBody(BaseModel):
    url: str
    refresh: bool = False
    idle_timeout: float = Field(default=8, ge=1, le=60)
    headless: bool = False

class CollectionBody(BaseModel):
    name: str

class SelectBody(BaseModel):
    collection_id: int

class CommentsBody(BaseModel):
    min_comments: int = Field(default=2, ge=1, le=1000)
    limit: int = Field(default=0, ge=0, le=1000)
    headless: bool | None = None

class ResetCommentsBody(BaseModel):
    post_id: str = ""

_HTML = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

def create_app(
    state: AppState,
    store: Store,
    opener: Opener | None = None,
    comment_opener: CommentOpener | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state.attach()
        yield

    app = FastAPI(lifespan=lifespan)
    app.state.app_state = state
    app.state.store = store

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _HTML

    @app.get("/api/state")
    async def api_state() -> dict:
        return state.snapshot()

    @app.get("/api/posts")
    async def api_posts(page_url: str = "", q: str = "", offset: int = 0, limit: int = 50, collection_id: int = 0) -> dict:
        effective = collection_id or state.collection_id
        return store.list_posts(page_url=page_url, query=q, offset=offset, limit=min(limit, 200), collection_id=effective)

    @app.get("/api/collections")
    async def api_collections() -> dict:
        return {"collections": store.list_collections(), "current": state.collection_id}

    @app.post("/api/collections")
    async def api_create_collection(body: CollectionBody) -> dict:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="tên collection trống")
        collection = store.create_collection(name)
        return {"ok": True, "collection": collection}

    @app.delete("/api/collections/{collection_id}")
    async def api_delete_collection(collection_id: int):
        if state.feed_busy() or state.comment_running:
            raise HTTPException(status_code=409, detail="đang thu thập — dừng rồi hãy xóa collection")
        if collection_id == 1:
            raise HTTPException(status_code=400, detail="không thể xóa collection mặc định")
        cols_del, posts_del = store.delete_collection(collection_id)
        if state.collection_id == collection_id:
            fallback = store.get_collection(1)
            if fallback:
                state.collection_id = int(fallback["id"])
                state.collection_name = str(fallback["name"])
                state.emit_status()
        return {"ok": True, "deleted": {"collections": cols_del, "posts": posts_del}}

    @app.post("/api/select")
    async def api_select(body: SelectBody) -> dict:
        if state.feed_busy() or state.comment_running:
            raise HTTPException(status_code=409, detail="đang thu thập — dừng rồi hãy đổi collection")
        collection = store.get_collection(body.collection_id)
        if collection is None:
            raise HTTPException(status_code=404, detail="không có collection này")
        state.collection_id = collection["id"]
        state.collection_name = collection["name"]
        state.log(f"chuyển sang collection: {collection['name']}")
        state.emit_status()
        return {"ok": True}

    @app.post("/api/open")
    async def api_open(body: OpenBody) -> dict:
        if opener is None:
            raise HTTPException(status_code=404, detail="hãy chạy pagecrawl ui")
        url = body.url.strip()
        if not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail="URL phải bắt đầu bằng http:// hoặc https://")
        if not state.reserve_job(url, body.refresh, body.idle_timeout, body.headless):
            raise HTTPException(status_code=409, detail="đang có phiên crawl")
        state.log('mở Chromium ' + ('headless' if body.headless else 'có giao diện') + f' — {url}')
        state.job = asyncio.create_task(opener(url, body.refresh, body.idle_timeout, body.headless))
        return {"ok": True, "headless": body.headless}

    @app.post("/api/start")
    async def api_start() -> dict:
        state.attach()
        if state.status != "waiting_login":
            raise HTTPException(status_code=409, detail="chưa tới bước đăng nhập")
        state.log("bắt đầu crawl")
        state.start_event.set()
        return {"ok": True}

    @app.post("/api/stop")
    async def api_stop() -> dict:
        state.attach()
        if state.status in {"done", "error", "idle"}:
            if state.controls:
                return {"ok": True}
            state.log("đóng monitor")
            if state.shutdown_event is not None:
                state.shutdown_event.set()
            return {"ok": True}
        state.log("dừng")
        state.stop_reason = "user_stop"
        state.stop_event.set()
        state.start_event.set()
        return {"ok": True}

    @app.get("/api/comments/progress")
    async def api_comments_progress() -> dict:
        return store.comment_progress(state.collection_id)

    @app.get("/api/comments")
    async def api_comments(post_id: str = "", limit: int = 300, offset: int = 0) -> dict:
        if not post_id:
            raise HTTPException(status_code=400, detail="thiếu post_id")
        result = store.list_comments(state.collection_id, post_id, limit=min(limit, 1000), offset=offset)
        post = store.get_post(post_id, collection_id=state.collection_id)
        result["post"] = {
            "post_id": post_id,
            "author": (post or {}).get("author", ""),
            "comment_count": (post or {}).get("comment_count", 0),
            "comments_saved": (post or {}).get("comments_saved", 0),
            "comments_status": (post or {}).get("comments_status", "pending"),
            "permalink": (post or {}).get("permalink"),
        }
        return result

    @app.post("/api/comments/start")
    async def api_comments_start(body: CommentsBody) -> dict:
        if comment_opener is None:
            raise HTTPException(status_code=404, detail="hãy chạy pagecrawl ui")
        if state.comment_running:
            raise HTTPException(status_code=409, detail="đang lấy bình luận")
        if state.feed_busy():
            raise HTTPException(status_code=409, detail="dừng lấy bài viết trước khi lấy bình luận")
        state.attach()
        if state.comment_stop_event is not None:
            state.comment_stop_event.clear()
        progress = store.comment_progress(state.collection_id, body.min_comments)
        if progress["pending"] == 0:
            raise HTTPException(status_code=400, detail="không còn bài nào cần lấy bình luận")
        state.comment_running = True
        if body.headless is not None:
            state.headless = body.headless
        state.comment_total = progress["pending"]
        state.comment_done = 0
        state.comment_error = 0
        state.comment_current = ""
        state.log(
            f"bắt đầu lấy bình luận: {progress['pending']} bài trong "
            f"collection {state.collection_name}"
        )
        state.emit_status()
        state.comment_job = asyncio.create_task(
            comment_opener(state.collection_id, body.min_comments, body.limit or None)
        )
        return {"ok": True, "pending": progress["pending"]}

    @app.post("/api/comments/stop")
    async def api_comments_stop() -> dict:
        state.attach()
        if not state.comment_running:
            return {"ok": True}
        state.log("dừng lấy bình luận — bài đang làm sẽ xong rồi mới dừng")
        if state.comment_stop_event is not None:
            state.comment_stop_event.set()
        return {"ok": True}

    @app.post("/api/comments/reset")
    async def api_comments_reset(body: ResetCommentsBody) -> dict:
        if state.comment_running or state.feed_busy():
            raise HTTPException(status_code=409, detail="đang thu thập — dừng trước đã")
        changed = store.reset_comments(state.collection_id, body.post_id or None)
        state.log(f"đã xoá bình luận và đặt lại trạng thái cho {changed} bài")
        return {"ok": True, "reset": changed}

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        queue = asyncio.Queue()
        state.subscribers.append(queue)

        async def stream():
            try:
                yield _sse(state.snapshot())
                while True:
                    if await request.is_disconnected():
                        break
                    event = await queue.get()
                    yield _sse(event)
            finally:
                if queue in state.subscribers:
                    state.subscribers.remove(queue)

        return StreamingResponse(stream(), media_type="text/event-stream")

    return app

def _sse(event: dict) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _config(app: object, port: int) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        timeout_graceful_shutdown=2,
    )

async def run_crawl_job(
    state: AppState,
    store: Store,
    profile_path,
    idle_timeout: float,
) -> None:

    source = PlaywrightSource(profile_path, headless=state.headless)
    watcher = None
    try:
        await source.start(state.page_url)
        state.page = source.page
        state.set_status("waiting_login")
        mode = "headless" if source.headless else "có giao diện"
        state.log(f"đã mở Chromium ({mode}) — đăng nhập nếu cần, rồi bấm Bắt đầu crawl")
        watcher = asyncio.create_task(_watch_session(source, state))
        await state.start_event.wait()
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        if state.stop_event.is_set() and state.status == "waiting_login":
            state.stop_reason = state.stop_reason or "user_stop"
            state.set_status("done")
        else:
            await crawl_feed(source, store, state, idle_timeout=idle_timeout)
        _finish(store, state, state.status)
        state.log(
            f"xong: mới={state.new_count} cập nhật={state.updated_count} bỏ qua={state.skip_count} ({state.stop_reason})"
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        state.stop_reason = str(exc)
        state.set_status("error")
        state.log(f"lỗi: {exc}")
        _finish(store, state, "error")
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        await source.close()
        state.page = None

def _finish(store: Store, state: AppState, status: str) -> None:
    if state.run_id is None:
        return
    store.finish_run(
        state.run_id,
        status,
        state.stop_reason,
        state.new_count,
        state.skip_count,
        state.updated_count,
        state.rounds,
    )

async def run_session(
    page_url: str,
    db_path,
    profile_path,
    *,
    refresh: bool = False,
    port: int = 8787,
    headless: bool = False,
    idle_timeout: float = 8.0,
    keep_monitor: bool = True,
    state: AppState | None = None,
    collection: str = "mặc định",
) -> int:
    store = Store(db_path)
    state = state or AppState()
    state.attach()
    state.controls = False
    col = store.create_collection(collection)
    state.collection_id = col["id"]
    state.collection_name = col["name"]
    state.page_url = page_url
    state.refresh = refresh
    state.idle_timeout = idle_timeout
    state.headless = headless
    state.run_id = store.create_run(page_url)
    app = create_app(state, store)
    server = uvicorn.Server(_config(app, port))
    server_task = asyncio.create_task(server.serve())
    exit_code = 0
    try:
        await _wait_until_started(server, port)
        state.log(f"monitor http://127.0.0.1:{port}")
        await run_crawl_job(state, store, profile_path, idle_timeout)
        if state.status == "error":
            exit_code = 1
        aborted_early = state.stop_reason == "user_stop" and state.rounds == 0 and state.new_count == 0
        if keep_monitor and state.shutdown_event is not None and not aborted_early:
            state.log("Monitor vẫn mở. Bấm Dừng hoặc nhấn Ctrl+C để thoát.")
            await state.shutdown_event.wait()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        exit_code = 1
        state.stop_reason = str(exc)
        state.set_status("error")
        state.log(f"lỗi: {exc}")
        _finish(store, state, "error")
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await server_task
        store.close()
    return exit_code

def _make_comment_opener(state: AppState, store: Store, profile_path):

    async def opener(collection_id: int, min_comments: int, limit: int | None) -> None:
        source: PlaywrightSource | None = None
        try:
            def say(line: str) -> None:
                state.log(line)
                state.comment_current = line
                progress_now = store.comment_progress(collection_id, min_comments)
                state.comment_done = max(0, progress_now["done"] - initial_done)
                state.comment_saved = progress_now["saved"]
                state.comment_error = progress_now["error"]
                state.emit_status()

            progress = store.comment_progress(collection_id, min_comments)
            initial_done = progress["done"]
            state.comment_total = min(progress["pending"], limit) if limit else progress["pending"]
            source = PlaywrightSource(profile_path, headless=state.headless)
            await source.start("https://www.facebook.com/")
            done = await crawl_comments(
                source,
                store,
                collection_id=collection_id,
                min_comments=min_comments,
                limit=limit,
                stop=state.should_stop_comments,
                on_progress=say,
            )
            state.comment_done = done
            state.comment_error = store.comment_progress(collection_id, min_comments)["error"]
            state.log(f"lấy bình luận xong: {done} bài")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.log(f"lỗi lấy bình luận: {exc}")
        finally:
            if source is not None:
                await source.close()
            state.comment_running = False
            state.comment_current = ""
            state.emit_status()

    return opener

async def serve_ui(db_path, profile_path, *, port: int = 8787, headless: bool = False) -> None:
    store = Store(db_path)
    state = AppState()
    state.attach()
    state.controls = True
    state.headless = headless
    default_col = store.get_collection(state.collection_id)
    if default_col is not None:
        state.collection_name = default_col["name"]
    state.set_status("idle")

    async def opener(url: str, refresh: bool, idle_timeout: float, headless: bool) -> None:
        try:
            state.headless = headless
            state.run_id = store.create_run(url)
            await run_crawl_job(state, store, profile_path, idle_timeout)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            state.stop_reason = str(exc)
            state.set_status("error")
            state.log(f"lỗi: {exc}")

    app = create_app(
        state,
        store,
        opener=opener,
        comment_opener=_make_comment_opener(state, store, profile_path),
    )
    server = uvicorn.Server(_config(app, port))
    try:
        await server.serve()
    finally:
        jobs = [job for job in (state.job, state.comment_job) if job is not None and not job.done()]
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        store.close()

async def _wait_until_started(server: uvicorn.Server, port: int) -> None:
    started = server.started
    if hasattr(started, "wait"):
        await asyncio.wait_for(started.wait(), 10)
        return
    for _ in range(200):
        if server.started:
            return
        await asyncio.sleep(0.05)
    raise RuntimeError(f"không mở được monitor tại cổng {port}")

async def _watch_session(source: PlaywrightSource, state: AppState) -> None:
    last = None
    try:
        while not state.start_event.is_set():
            ready = await source.session_hint()
            if ready != last:
                last = ready
                state.session_ready = ready
                state.log("session còn — feed đã hiện" if ready else "chưa thấy feed (đăng nhập nếu cần)")
                state.emit_status()
            await asyncio.sleep(1)
    except asyncio.CancelledError:
        return


app = typer.Typer(add_completion=False, help="Crawl bài viết đang xem được trên một page.")

def _utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            continue

_utf8_stdio()

def _launch_ui(port: int, db: Path, profile: Path, headless: bool) -> None:
    typer.echo(f"Mở http://127.0.0.1:{port}")
    typer.echo("Mọi thao tác crawl nằm trên trang này. Thoát bằng Ctrl+C.")
    asyncio.run(serve_ui(db, profile, port=port, headless=headless))

@app.callback(invoke_without_command=True)
def _setup(ctx: typer.Context) -> None:
    _utf8_stdio()
    if ctx.invoked_subcommand is None:
        _launch_ui(8787, _db_option(), _profile_option(), False)

def _db_option() -> Path:
    return Path("data") / "crawler.db"

def _profile_option() -> Path:
    return Path("data") / "profile"

@app.command()
def ui(
    port: int = typer.Option(8787, "--port", help="Cổng giao diện"),
    db: Path = typer.Option(_db_option(), "--db"),
    profile: Path = typer.Option(_profile_option(), "--profile"),
    headless: bool = typer.Option(False, "--headless", help="Ẩn cửa sổ Chromium. Mặc định là hiện."),
) -> None:
    """Khởi động giao diện điều khiển thu thập dữ liệu."""
    _launch_ui(port, db, profile, headless)

@app.command()
def run(
    url: str = typer.Option(..., "--url", help="URL page cần crawl"),
    refresh: bool = typer.Option(False, "--refresh", help="Cập nhật bài đã có nếu like, comment, text hoặc ảnh đổi"),
    db: Path = typer.Option(_db_option(), "--db", help="File SQLite"),
    profile: Path = typer.Option(_profile_option(), "--profile", help="Thư mục profile trình duyệt"),
    port: int = typer.Option(8787, "--port", help="Cổng web monitor"),
    headless: bool = typer.Option(False, "--headless", help="Chạy không giao diện. Mặc định là có cửa sổ."),
    idle_timeout: float = typer.Option(8.0, "--idle-timeout", help="Giây chờ mỗi vòng scroll"),
    collection: str = typer.Option("mặc định", "--collection", help="Collection lưu bài viết (tạo mới nếu chưa có)"),
) -> None:
    if not url.startswith(("http://", "https://")):
        raise typer.BadParameter("URL phải bắt đầu bằng http:// hoặc https://")
    typer.echo(f"Monitor: http://127.0.0.1:{port}")
    typer.echo("Đăng nhập trên cửa sổ trình duyệt nếu site yêu cầu, rồi bấm Bắt đầu crawl.")
    code = asyncio.run(
        run_session(
            url,
            db,
            profile,
            refresh=refresh,
            port=port,
            headless=headless,
            idle_timeout=idle_timeout,
            collection=collection,
        )
    )
    if code:
        raise typer.Exit(code)

@app.command()
def comments(
    collection: str = typer.Option("mặc định", "--collection", help="Collection cần lấy bình luận"),
    min_comments: int = typer.Option(2, "--min-comments", help="Chỉ lấy bài có số bình luận từ mức này trở lên"),
    db: Path = typer.Option(_db_option(), "--db"),
    profile: Path = typer.Option(_profile_option(), "--profile"),
    headless: bool = typer.Option(True, "--headless/--headed", help="Ẩn cửa sổ hay hiện cửa sổ Chromium"),
    limit: int = typer.Option(0, "--limit", help="Giới hạn số bài. Mặc định 0 = lấy hết cả collection"),
    retries: int = typer.Option(
        2,
        "--retries",
        help="Số lần tải lại bài khi chưa lấy đủ. 0 = nhanh nhất nhưng thiếu bình luận.",
    ),
) -> None:

    async def _run() -> int:
        store = Store(db)
        try:
            target = store.get_collection_by_name(collection)
            if target is None:
                typer.echo(f"không có collection: {collection}")
                typer.echo("có sẵn: " + ", ".join(c["name"] for c in store.list_collections()))
                return 2
            collection_id = int(target["id"])
            progress = store.comment_progress(collection_id, min_comments)
            typer.echo(f"collection: {collection} | bài đủ ngưỡng: {progress['eligible']}")
            typer.echo(f"đã lấy xong: {progress['done']} | chờ lấy: {progress['pending']} | lỗi: {progress['error']}")
            if progress["pending"] == 0:
                typer.echo("không còn bài nào cần lấy bình luận.")
                return 0
            so_chay = min(progress["pending"], limit) if limit > 0 else progress["pending"]
            typer.echo(f"sẽ lấy {so_chay} bài (ước khoảng {so_chay * 25 // 60} phút). "
                       f"Thêm --limit N để giới hạn.")
            if so_chay >= 300:
                typer.echo("LƯU Ý: số bài khá nhiều, nên chia ra chạy vài lần.")

            source = PlaywrightSource(profile, headless=headless)
            await source.start("https://www.facebook.com/")
            try:
                await crawl_comments(
                    source,
                    store,
                    collection_id=collection_id,
                    min_comments=min_comments,
                    limit=limit or None,
                    retries=retries,
                    on_progress=typer.echo,
                )
            finally:
                await source.close()
            after = store.comment_progress(collection_id, min_comments)
            typer.echo(
                f"xong: lưu {after['saved']} bình luận | bài đã lấy {after['done']}/{after['eligible']}"
            )
            return 0
        finally:
            store.close()

    code = asyncio.run(_run())
    if code:
        raise typer.Exit(code)

@app.command()
def report(
    url: str = typer.Option(..., "--url", help="URL page đã crawl"),
    db: Path = typer.Option(_db_option(), "--db"),
    collection: str = typer.Option("", "--collection", help="Chỉ xem bài trong collection này"),
) -> None:
    store = Store(db)
    try:
        collection_id = None
        if collection:
            match = next((c for c in store.list_collections() if c["name"] == collection), None)
            if match is None:
                raise typer.BadParameter(f"không có collection: {collection}")
            collection_id = match["id"]
        result = store.list_posts(page_url=url, limit=10000, collection_id=collection_id)
    finally:
        store.close()
    typer.echo(f"{result['total']} bài cho {url}")
    for post in reversed(result["posts"]):
        preview = " ".join(post["text"].split())
        if len(preview) > 80:
            preview = preview[:77] + "..."
        when = post["created_at"] or post["time_label"] or ""
        link = post["permalink"] or "(không có link)"
        typer.echo(
            f"- {post['author']} | {when} | like={post['like_count']} comment={post['comment_count']}"
            f" | ảnh={len(post['image_urls'])} | {link}"
        )
        typer.echo(f"    {preview}")


if __name__ == "__main__":
    app()
