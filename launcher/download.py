"""Robust downloads: streaming, hash verification, retries, thread pool."""

import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

import requests

USER_AGENT = "NPLauncher/1.0 (+https://github.com/np-launcher) requests"

_HASH_ATTR = {"sha1": hashlib.sha1, "sha512": hashlib.sha512, "md5": hashlib.md5}


class DownloadError(RuntimeError):
    pass


def _hash_file(path: str, algo: str) -> str:
    h = _HASH_ATTR[algo]()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower()


def file_ok(path: str, sha1: str | None = None, sha512: str | None = None) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        if sha1 and _hash_file(path, "sha1") != sha1.lower():
            return False
        if sha512 and _hash_file(path, "sha512") != sha512.lower():
            return False
        return True
    except OSError:
        return False


def download_file(
    url: str,
    dest: str,
    *,
    sha1: str | None = None,
    sha512: str | None = None,
    expected_size: int | None = None,
    timeout: int = 60,
    retries: int = 3,
) -> str:
    """Download url -> dest (atomic). Skips if a verified copy already exists."""
    dest = os.path.abspath(dest)
    if file_ok(dest, sha1, sha512):
        return dest
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".part"

    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with requests.get(
                url, stream=True, timeout=timeout, headers={"User-Agent": USER_AGENT}
            ) as resp:
                resp.raise_for_status()
                h_sha1 = _HASH_ATTR["sha1"]()
                h_sha512 = _HASH_ATTR["sha512"]()
                size = 0
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(1 << 16):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        h_sha1.update(chunk)
                        h_sha512.update(chunk)
                        size += len(chunk)
            if expected_size and size != int(expected_size):
                raise DownloadError(f"size mismatch for {url}: {size} != {expected_size}")
            if sha1 and h_sha1.hexdigest().lower() != sha1.lower():
                raise DownloadError(f"sha1 mismatch for {url}")
            if sha512 and h_sha512.hexdigest().lower() != sha512.lower():
                raise DownloadError(f"sha512 mismatch for {url}")
            os.replace(tmp, dest)
            return dest
        except Exception as exc:  # noqa: BLE001 — retried, then surfaced
            last_err = exc
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            if attempt < retries:
                time.sleep(0.6 * attempt)
    raise DownloadError(f"failed to download {url}: {last_err}")


def download_many(
    items: Iterable[dict],
    *,
    workers: int = 12,
    on_item: Callable[[int, int], None] | None = None,
) -> list[str]:
    """Download many files concurrently.

    items: dicts with keys url, dest, optional sha1, sha512, size.
    Returns list of dests. Raises DownloadError listing all failures.
    """
    items = list(items)
    total = len(items)
    done = 0
    errors: list[str] = []
    lock = threading.Lock()

    def work(item):
        return download_file(
            item["url"],
            item["dest"],
            sha1=item.get("sha1"),
            sha512=item.get("sha512"),
            expected_size=item.get("size"),
        )

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, it): it for it in items}
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                fut.result()
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(f"{item.get('dest', item.get('url'))}: {exc}")
            finally:
                if on_item:
                    with lock:
                        done += 1
                        cur = done
                    on_item(cur, total)
    if errors:
        raise DownloadError(f"{len(errors)} download(s) failed:\n" + "\n".join(errors[:10]))
    return [it["dest"] for it in items]
