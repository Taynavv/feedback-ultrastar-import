"""Import UltraStar — backend routes.

Bulk-imports UltraStar karaoke song folders into feedpaks with a playable Vocals
arrangement (see convert.py). The songs live on the local disk, so the flow is
scan-a-folder → pick songs → convert each into the DLC folder and index it.

Endpoints:
  GET  /api/plugins/ultrastar_import/status  — ffmpeg availability + last used dir
  POST /api/plugins/ultrastar_import/scan    — list importable songs in a folder
  POST /api/plugins/ultrastar_import/import_start — stash a selection, get a job id
  WS   /ws/plugins/ultrastar_import/import   — run the job, streaming progress
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
import sys
import threading
import time
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from convert import ConvertError, convert, find_tool, pick_chart  # noqa: E402
from ultrastar_parse import parse_file  # noqa: E402

# merge.py needs numpy (auto-installed from requirements.txt by the plugin
# loader) — imported lazily so a missing dep degrades to import-only.

_OUTPUT_SUBDIR = "sloppack"
_JOB_TTL_SECONDS = 3600

_get_dlc_dir = None
_extract_meta = None
_meta_db = None
_log = None
_config_dir: Path | None = None

# job token -> (root dir, [folder names], created-at monotonic)
_jobs: dict[str, tuple[Path, list[str], float]] = {}


def _config_file() -> Path | None:
    if _config_dir is None:
        return None
    return Path(_config_dir) / "ultrastar_import.json"


def _load_config() -> dict:
    cfg = _config_file()
    try:
        if cfg and cfg.is_file():
            return json.loads(cfg.read_text(encoding="utf-8"))
    except Exception:
        _log.warning("could not read plugin config", exc_info=True)
    return {}


def _save_config(data: dict) -> None:
    cfg = _config_file()
    if not cfg:
        return
    try:
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        _log.warning("could not write plugin config", exc_info=True)


def _safe(part: str, limit: int) -> str:
    return re.sub(r"[<>:\"/\\|?*\s]", "_", part)[:limit].strip("_")


def _out_name(title: str, artist: str) -> str:
    """Output pak filename; the '_us' tag marks UltraStar provenance and drives
    the already-imported check."""
    safe_t = _safe(title, 60) or "song"
    safe_a = _safe(artist, 40)
    return f"{safe_t}_{safe_a}_us.feedpak" if safe_a else f"{safe_t}_us.feedpak"


def _normalize(s: str) -> str:
    """Match key for artist/title (mirrors merge.normalize, numpy-free)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = re.sub(r"[^a-z0-9 ]+", " ", s.casefold())
    return re.sub(r"\s+", " ", s).strip()


def _library_paks(dlc: Path) -> list[dict]:
    """Every non-UltraStar pak in the library, with match keys and vocals state."""
    import zipfile

    import yaml
    out = []
    for pak in sorted(dlc.rglob("*.feedpak")) + sorted(dlc.rglob("*.sloppak")):
        if pak.name.endswith("_us.feedpak"):
            continue  # our own standalone imports are not merge targets
        try:
            with zipfile.ZipFile(pak) as z:
                m = yaml.safe_load(z.read("manifest.yaml"))
            out.append({
                "artist": _normalize(str(m.get("artist", ""))),
                "title": _normalize(str(m.get("title", ""))),
                "path": pak,
                "rel": pak.relative_to(dlc).as_posix(),
                "display": f"{m.get('artist', '?')} — {m.get('title', '?')}",
                "has_vocals": any(a.get("id") == "vocals"
                                  for a in (m.get("arrangements") or [])
                                  if isinstance(a, dict)),
            })
        except Exception:
            continue
    return out


def _find_candidate(paks: list[dict], artist: str, title: str) -> dict | None:
    na, nt = _normalize(artist), _normalize(title)
    if not nt:
        return None
    for p in paks:
        if nt == p["title"] and (na == p["artist"] or not na or not p["artist"]):
            return p
    return None


def _scan_songs(root: Path) -> dict:
    """Walk one level of song folders; parse each chart for list metadata."""
    dlc = _get_dlc_dir()
    out_dir = (Path(dlc) / _OUTPUT_SUBDIR) if dlc else None
    paks = _library_paks(Path(dlc)) if dlc else []
    songs, errors = [], []
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        try:
            chart = pick_chart(folder)
        except ConvertError:
            continue  # no chart — not an UltraStar song folder
        try:
            song = parse_file(chart)
            title = song.title or folder.name
            artist = song.artist or ""
            name = _out_name(title, artist)
            has_audio = bool(
                sorted(folder.glob("*.mp3"))
                or (song.headers.get("MP3") and (folder / song.headers["MP3"].strip()).is_file())
            )
            cand = _find_candidate(paks, artist, title)
            songs.append({
                "folder": folder.name,
                "title": title,
                "artist": artist,
                "language": song.headers.get("LANGUAGE", ""),
                "year": song.headers.get("YEAR", ""),
                "is_duet": song.is_duet,
                "has_audio": has_audio,
                "imported": bool(out_dir and (out_dir / name).is_file()),
                "merge_into": cand["display"] if cand else None,
                "merged": bool(cand and cand["has_vocals"]),
            })
        except Exception as exc:  # noqa: BLE001
            errors.append({"folder": folder.name, "error": str(exc)})
    return {"songs": songs, "errors": errors}


_BAK_SUFFIX = ".pre-merge.bak"


def _reindex_pak(path: Path, rel_name: str) -> None:
    try:
        meta = _extract_meta(path)
        stat = path.stat()
        _meta_db.put(rel_name, stat.st_mtime, stat.st_size, meta)
    except Exception:
        _log.warning("metadata indexing failed for %r", rel_name, exc_info=True)


def _list_backups() -> list[dict]:
    """Every .pre-merge.bak in the library, with display metadata."""
    import zipfile

    import yaml
    dlc = _get_dlc_dir()
    if not dlc:
        return []
    out = []
    for bak in sorted(Path(dlc).rglob(f"*{_BAK_SUFFIX}")):
        st = bak.stat()
        entry = {"rel": bak.relative_to(dlc).as_posix(),
                 "size": st.st_size, "mtime": st.st_mtime}
        try:
            with zipfile.ZipFile(bak) as z:
                m = yaml.safe_load(z.read("manifest.yaml"))
            entry["display"] = f"{m.get('artist', '?')} — {m.get('title', '?')}"
        except Exception:
            entry["display"] = bak.name
        out.append(entry)
    return out


def _resolve_backup(rel: str) -> Path | None:
    """Client-supplied backup relpath → verified path inside the DLC dir."""
    dlc = _get_dlc_dir()
    if not dlc or not str(rel).endswith(_BAK_SUFFIX):
        return None
    root = Path(dlc).resolve()
    p = (root / rel).resolve()
    if root not in p.parents:
        return None
    return p if p.is_file() else None


def _purge_stale_jobs() -> None:
    now = time.monotonic()
    for token in [t for t, entry in _jobs.items() if now - entry[-1] > _JOB_TTL_SECONDS]:
        _jobs.pop(token, None)


def setup(app, context):
    global _get_dlc_dir, _extract_meta, _meta_db, _log, _config_dir
    _get_dlc_dir = context["get_dlc_dir"]
    _extract_meta = context["extract_meta"]
    _meta_db = context["meta_db"]
    _log = context["log"]
    _config_dir = context["config_dir"]

    @app.get("/api/plugins/ultrastar_import/status")
    async def us_status():
        try:
            find_tool("ffmpeg")
            find_tool("ffprobe")
            ffmpeg_ok, ffmpeg_hint = True, None
        except ConvertError as exc:
            ffmpeg_ok, ffmpeg_hint = False, str(exc)
        try:
            import numpy  # noqa: F401 — merge dep, from requirements.txt
            merge_ok = True
        except Exception:
            merge_ok = False
        return {
            "ffmpeg_available": ffmpeg_ok,
            "ffmpeg_hint": ffmpeg_hint,
            "merge_available": merge_ok,
            "last_dir": _load_config().get("last_dir"),
        }

    @app.post("/api/plugins/ultrastar_import/scan")
    async def us_scan(data: dict):
        raw = str(data.get("dir", "")).strip()
        if not raw:
            return {"error": "No folder given"}
        root = Path(raw)
        if not root.is_dir():
            return {"error": f"Not a folder: {raw}"}
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _scan_songs, root)
        if result["songs"] or not result["errors"]:
            _save_config({"last_dir": str(root)})
        result["dir"] = str(root)
        return result

    @app.get("/api/plugins/ultrastar_import/backups")
    async def us_backups():
        loop = asyncio.get_running_loop()
        backups = await loop.run_in_executor(None, _list_backups)
        return {"backups": backups, "total_size": sum(b["size"] for b in backups)}

    @app.post("/api/plugins/ultrastar_import/backup_restore")
    async def us_backup_restore(data: dict):
        bak = _resolve_backup(str(data.get("rel", "")))
        if bak is None:
            return {"error": "Unknown backup"}
        pak = Path(str(bak)[: -len(_BAK_SUFFIX)])
        try:
            bak.replace(pak)
        except OSError as exc:
            return {"error": f"Restore failed: {exc}"}
        dlc = Path(_get_dlc_dir())
        _reindex_pak(pak, pak.relative_to(dlc).as_posix())
        _log.info("restored %s from pre-merge backup", pak.name)
        return {"ok": True, "restored": pak.name}

    @app.post("/api/plugins/ultrastar_import/backup_delete")
    async def us_backup_delete(data: dict):
        if data.get("all"):
            deleted = 0
            for b in _list_backups():
                p = _resolve_backup(b["rel"])
                if p is not None:
                    try:
                        p.unlink()
                        deleted += 1
                    except OSError:
                        _log.warning("could not delete %s", p.name, exc_info=True)
            return {"ok": True, "deleted": deleted}
        bak = _resolve_backup(str(data.get("rel", "")))
        if bak is None:
            return {"error": "Unknown backup"}
        try:
            bak.unlink()
        except OSError as exc:
            return {"error": f"Delete failed: {exc}"}
        return {"ok": True, "deleted": 1}

    @app.post("/api/plugins/ultrastar_import/import_start")
    async def us_import_start(data: dict):
        raw = str(data.get("dir", "")).strip()
        folders = data.get("folders") or []
        merge_enabled = bool(data.get("merge", True))
        root = Path(raw)
        if not root.is_dir():
            return {"error": f"Not a folder: {raw}"}
        if not isinstance(folders, list) or not folders:
            return {"error": "No songs selected"}
        if not _get_dlc_dir():
            return {"error": "DLC folder not configured"}
        _purge_stale_jobs()
        token = secrets.token_hex(16)
        _jobs[token] = (root, [str(f) for f in folders], merge_enabled,
                        time.monotonic())
        return {"job": token, "count": len(folders)}

    @app.websocket("/ws/plugins/ultrastar_import/import")
    async def ws_import(websocket: WebSocket, job: str):
        await websocket.accept()
        entry = _jobs.pop(job, None)
        if entry is None or time.monotonic() - entry[3] > _JOB_TTL_SECONDS:
            await websocket.send_json({"error": "Import session expired — rescan and retry"})
            await websocket.close()
            return
        root, folders, merge_enabled, _ = entry

        dlc = _get_dlc_dir()
        if not dlc:
            await websocket.send_json({"error": "DLC folder not configured"})
            await websocket.close()
            return
        out_dir = Path(dlc) / _OUTPUT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)

        progress_queue: asyncio.Queue = asyncio.Queue()
        cancel = threading.Event()

        def _run_job():
            done = failed = merged = 0
            total = len(folders)
            merge_mod = None
            if merge_enabled:
                try:
                    import merge as merge_mod  # lazy: needs numpy (requirements.txt)
                except Exception:
                    _log.warning("merge module unavailable — importing standalone",
                                 exc_info=True)
            paks = _library_paks(Path(dlc)) if merge_mod else []

            for i, folder_name in enumerate(folders):
                if cancel.is_set():
                    break
                src = root / folder_name
                msg_base = {"i": i, "total": total, "folder": folder_name}
                try:
                    chart = pick_chart(src)
                    song = parse_file(chart)
                    title = song.title or folder_name
                    artist = song.artist or ""
                    out_name = _out_name(title, artist)
                    out_path = out_dir / out_name
                    info = {**msg_base, "title": title, "artist": artist}

                    cand = (_find_candidate(paks, artist, title)
                            if merge_mod else None)
                    if cand is not None:
                        # Same recording already in the library → graft the vocal
                        # chart into that pak instead of creating a duplicate.
                        progress_queue.put_nowait(
                            {**info, "stage": "merging", "target": cand["display"]})
                        try:
                            merge_mod.merge(
                                src, cand["path"], apply=True,
                                log=lambda m: _log.info("[%s] %s", folder_name, m))
                            _reindex_pak(cand["path"], cand["rel"])
                            if out_path.is_file():
                                out_path.unlink()  # standalone copy now redundant
                                _log.info("[%s] removed standalone %s after merge",
                                          folder_name, out_name)
                            merged += 1
                            done += 1
                            progress_queue.put_nowait(
                                {**info, "stage": "done",
                                 "merged_into": cand["display"]})
                            continue
                        except merge_mod.MergeError as exc:
                            # Not the same recording (or uncertain) — keep the
                            # song, but as its own pak.
                            progress_queue.put_nowait(
                                {**info, "stage": "kept-separate",
                                 "message": str(exc)})

                    progress_queue.put_nowait({**info, "stage": "converting"})
                    convert(src, out_path,
                            log=lambda m: _log.info("[%s] %s", folder_name, m))
                    # POSIX form — the core's own scanner keys entries with
                    # forward slashes; a backslash key would shadow-duplicate.
                    _reindex_pak(out_path, (Path(_OUTPUT_SUBDIR) / out_name).as_posix())
                    done += 1
                    progress_queue.put_nowait(
                        {**info, "stage": "done", "filename": out_name})
                except Exception as exc:  # noqa: BLE001
                    _log.exception("UltraStar import failed for %s", folder_name)
                    failed += 1
                    # NB: key is "message", not "error" — "error" aborts the whole
                    # websocket loop; a single bad song must not stop the batch.
                    progress_queue.put_nowait(
                        {**msg_base, "stage": "failed", "message": str(exc)})
            progress_queue.put_nowait(
                {"done": True, "imported": done, "merged": merged,
                 "failed": failed, "total": total, "cancelled": cancel.is_set()})

        loop = asyncio.get_running_loop()
        job_task = loop.run_in_executor(None, _run_job)

        try:
            while True:
                try:
                    msg = await asyncio.wait_for(progress_queue.get(), timeout=1.0)
                    await websocket.send_json(msg)
                    if msg.get("done") or msg.get("error"):
                        break
                except asyncio.TimeoutError:
                    if job_task.done():
                        break
        except WebSocketDisconnect:
            cancel.set()
        try:
            await websocket.close()
        except Exception:
            pass
