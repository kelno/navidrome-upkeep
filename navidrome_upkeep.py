#!/usr/bin/env python3
"""Upkeep of a Navidrome library fed by Explo: keep the starred discovery tracks, trash the tracks of a delete playlist.

Explo (https://github.com/LumePart/Explo) downloads a weekly discovery playlist into a Navidrome library of its own, and
never deletes anything. This script, run with its TOML settings file as argument (see `Settings`), does the rest:

1. Log in to Navidrome as the user in the login file (Explo's own env file), whose stars and playlists count.
2. Move every starred track of the discovery library to the keep folder, inside the main library, keeping its
   subfolders.
3. If a delete playlist is set: move every track of that playlist of the user, whatever its library, to today's trash
   folder, at its path on the server (`<trash>/2026-11-20/mnt/music/Artist/x.flac`), then take it out of the playlist.
4. Weekly only: once every move worked, delete everything left in the discovery folder. Any failure stops the script
   before it deletes anything.
5. Rescan only the folders that changed, so a large main library isn't walked. A track moved between libraries within
   one scan stays the same track for Navidrome.
6. Weekly only: remove the deleted tracks from Navidrome's missing files.
7. Delete the trash folders older than the trash delay, removing their tracks from Navidrome's missing files only then:
   until that point, a track moved back to its old path gets its stars and play counts back. This relies on Navidrome
   keeping missing files (`Scanner.PurgeMissing = "never"`, its default).

Two modes:
- weekly (no flag), run just before Explo: every step above;
- `--frequent`, run every few minutes by a timer: every step but the weekly ones. Does nothing while the weekly service
  runs or Navidrome scans, so it never races Explo or a scan; the next run catches up.

Standard library only: it runs on the server's own Python.
"""

import argparse
import dataclasses
import datetime
import errno
import fcntl
import hashlib
import json
import logging
import pathlib
import secrets
import shutil
import subprocess
import time
import tomllib
import typing
import urllib.parse
import urllib.request

logger = logging.getLogger("navidrome-upkeep")

# Client name Navidrome shows for this script
CLIENT_NAME = "navidrome-upkeep"
SUBSONIC_API_VERSION = "1.16.1"
HTTP_TIMEOUT_SECONDS = 60
SCAN_POLL_SECONDS = 10
# Navidrome's API pages its lists: one page is enough for a playlist or the missing files
PAGE_END = 5000
# Keys of the login file, shared with Explo
USERNAME_KEY = "SYSTEM_USERNAME"
PASSWORD_KEY = "SYSTEM_PASSWORD"  # noqa: S105 (the name of the key, not a password)
SYSTEMCTL = "/usr/bin/systemctl"
# States of the weekly service in which `--frequent` runs
IDLE_SERVICE_STATES = ("inactive", "failed")

type Json = dict[str, object]


@dataclasses.dataclass(frozen=True)
class Settings:
    """The TOML settings file. Every key is required except `delete_playlist` and `trash_path`, set together or not."""

    # Navidrome's address, as reached from where the script runs
    navidrome_url: str
    # Env file with the Navidrome login, the one Explo reads
    login_file: pathlib.Path
    # The discovery library: the same path for this script and for Navidrome
    discovery_path: pathlib.Path
    # Folder starred tracks are moved to, inside the main library
    keep_path: pathlib.Path
    # The main library's folder for this script, and as Navidrome sees it (in its container, say)
    main_library_path: pathlib.Path
    main_library_navidrome_path: str
    scan_timeout_minutes: int
    # The systemd service of the weekly run (Explo's), which `--frequent` waits out
    weekly_service: str
    # Held by every run, so two never overlap
    lock_file: pathlib.Path
    # Lists the folders waiting for their rescan, one `<library id>:<folder in the library>` per line: if a run fails
    # before rescanning, the next one does it
    pending_scan_file: pathlib.Path
    # Days a trashed track waits before it's deleted
    trash_days: int
    # The user's playlist whose tracks are trashed, and the trash folder, outside every library
    delete_playlist: str | None
    trash_path: pathlib.Path | None

    @classmethod
    def load(cls, path: pathlib.Path) -> typing.Self:
        """Read the settings file.

        Args:
            path: The TOML settings file.

        Returns:
            The settings.

        Raises:
            KeyError: A required key is missing.
            ValueError: Only one of `delete_playlist` and `trash_path` is set.
        """
        data = tomllib.loads(path.read_text())
        trash_path = data.get("trash_path")
        delete_playlist = data.get("delete_playlist")
        if (trash_path is None) != (delete_playlist is None):
            msg = f"{path}: set both delete_playlist and trash_path, or neither"
            raise ValueError(msg)
        return cls(
            navidrome_url=data["navidrome_url"],
            login_file=pathlib.Path(data["login_file"]),
            discovery_path=pathlib.Path(data["discovery_path"]),
            keep_path=pathlib.Path(data["keep_path"]),
            main_library_path=pathlib.Path(data["main_library_path"]),
            main_library_navidrome_path=data["main_library_navidrome_path"],
            scan_timeout_minutes=data["scan_timeout_minutes"],
            weekly_service=data["weekly_service"],
            lock_file=pathlib.Path(data["lock_file"]),
            pending_scan_file=pathlib.Path(data["pending_scan_file"]),
            trash_days=data["trash_days"],
            delete_playlist=delete_playlist,
            trash_path=pathlib.Path(trash_path) if trash_path is not None else None,
        )


class Navidrome:
    """Navidrome's own API and its Subsonic API, logged in as one user."""

    def __init__(self, url: str, username: str, password: str) -> None:
        """Log in to Navidrome's own API.

        Args:
            url: Navidrome's address.
            username: The user to log in as.
            password: Their password.

        Raises:
            urllib.error.URLError: Navidrome is unreachable or refused the login.
        """
        self.url: str = url.rstrip("/")
        self.username: str = username
        self.password: str = password
        # Empty until logged in: the login call itself carries no token
        self.token: str = ""
        response = self._request("POST", "/auth/login", body={"username": username, "password": password})
        self.token = typing.cast("str", typing.cast("Json", response)["token"])

    def _request(self, method: str, path: str, body: Json | None = None, query: list[tuple[str, str]] | None = None) -> object:
        """Call Navidrome's own API and return its decoded JSON answer, raising on any HTTP error."""
        url = f"{self.url}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)  # noqa: S310 (URL from the settings)
        request.add_header("Content-Type", "application/json")
        if self.token:
            request.add_header("X-ND-Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            content = response.read()
        return json.loads(content) if content else None

    def api(self, method: str, path: str, query: list[tuple[str, str]] | None = None) -> object:
        """Call Navidrome's own API as the logged-in user.

        Args:
            method: The HTTP method.
            path: The path, such as `/api/song`.
            query: Query parameters, repeated keys allowed.

        Returns:
            The decoded JSON answer, or None for an empty one.

        Raises:
            urllib.error.URLError: The call failed, an HTTP error included.
        """
        return self._request(method, path, query=query)

    def subsonic(self, endpoint: str, query: list[tuple[str, str]] | None = None) -> Json:
        """Call the Subsonic API with token authentication.

        Args:
            endpoint: The endpoint, such as `startScan`.
            query: Query parameters, repeated keys allowed.

        Returns:
            The `subsonic-response` object.

        Raises:
            urllib.error.URLError: The call failed.
            RuntimeError: Navidrome answered with an error.
        """
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode(), usedforsecurity=False).hexdigest()
        params = [("u", self.username), ("t", token), ("s", salt), ("v", SUBSONIC_API_VERSION), ("c", CLIENT_NAME), ("f", "json")]
        url = f"{self.url}/rest/{endpoint}?{urllib.parse.urlencode(params + (query or []))}"
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            answer = typing.cast("Json", typing.cast("Json", json.loads(response.read()))["subsonic-response"])
        if answer.get("status") != "ok":
            msg = f"Navidrome refused {endpoint}: {answer.get('error')}"
            raise RuntimeError(msg)
        return answer


def read_login(login_file: pathlib.Path) -> tuple[str, str]:
    """Read the Navidrome username and password from the env file shared with Explo.

    Args:
        login_file: The env file.

    Returns:
        The username and the password.

    Raises:
        RuntimeError: Either is missing.
    """
    values: dict[str, str] = {}
    for line in login_file.read_text().splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip()] = value.strip()
    username = values.get(USERNAME_KEY, "")
    password = values.get(PASSWORD_KEY, "")
    if not username or not password:
        msg = f"{login_file}: fill in {USERNAME_KEY} and {PASSWORD_KEY}, the Navidrome user whose stars count"
        raise RuntimeError(msg)
    return username, password


def library_folders(navidrome: Navidrome, settings: Settings) -> dict[int, pathlib.Path]:
    """Map each Navidrome library's id to its folder for this script.

    Navidrome may see the main library at another path (in its container); every other library is at the same path.

    Args:
        navidrome: The logged-in API.
        settings: The settings.

    Returns:
        Each library's folder, by library id.
    """
    libraries = typing.cast("list[Json]", navidrome.api("GET", "/api/library"))
    return {
        typing.cast("int", library["id"]): (
            settings.main_library_path
            if library["path"] == settings.main_library_navidrome_path
            else pathlib.Path(typing.cast("str", library["path"]))
        )
        for library in libraries
    }


def library_id(folders: dict[int, pathlib.Path], folder: pathlib.Path) -> int:
    """Return the id of the Navidrome library at this folder.

    Args:
        folders: Each library's folder, by library id.
        folder: The library's folder.

    Returns:
        The library's id.

    Raises:
        RuntimeError: No library has this folder.
    """
    for library, library_folder in folders.items():
        if library_folder == folder:
            return library
    known = ", ".join(str(library_folder) for library_folder in folders.values())
    msg = f"No Navidrome library at {folder} (libraries: {known}). Add it in Navidrome: Settings > Libraries > +"
    raise RuntimeError(msg)


def inside(path: pathlib.Path, folder: pathlib.Path) -> pathlib.Path:
    """Return the path resolved, refusing one that leads out of the folder.

    Args:
        path: The path to check.
        folder: The folder it must stay in.

    Returns:
        The resolved path.

    Raises:
        RuntimeError: The path leads out of the folder.
    """
    resolved = path.resolve()
    if not resolved.is_relative_to(folder.resolve()):
        msg = f"{path} is outside {folder}"
        raise RuntimeError(msg)
    return resolved


def today() -> datetime.date:
    """Today's date in the local time zone."""
    return datetime.datetime.now().astimezone().date()


def add_pending_scans(pending_scan: pathlib.Path, targets: list[str]) -> None:
    """Record folders to rescan, before changing any file in them.

    Args:
        pending_scan: The pending scans file.
        targets: Folders as `<library id>:<folder in the library>`.
    """
    with pending_scan.open("a") as file:
        file.writelines(f"{target}\n" for target in targets)


def move_file(source: pathlib.Path, target: pathlib.Path) -> None:
    """Move a file to a target that doesn't exist yet, creating its folders.

    Within one drive, a rename. Across drives, copy under a hidden name Navidrome skips, check the size, then rename:
    the keep folder and the trash may be on a remote drive, and an interrupted copy must not look like a finished one.
    The copy is of the contents only: on a drive whose files all belong to another user, such as an rclone mount, setting
    the mode or the times is refused (EPERM).

    Args:
        source: The file to move.
        target: Where it goes.

    Raises:
        OSError: The move failed.
        RuntimeError: The copy came out the wrong size.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        source.rename(target)
    except OSError as error:
        if error.errno != errno.EXDEV:
            raise
    else:
        return
    partial = target.with_name(f".{target.name}.part")
    shutil.copyfile(source, partial)
    if partial.stat().st_size != source.stat().st_size:
        msg = f"Copy of {source} to {partial} is incomplete"
        raise RuntimeError(msg)
    partial.replace(target)
    source.unlink()


def keep_starred(navidrome: Navidrome, discovery_id: int, settings: Settings, scan_targets: list[str]) -> None:
    """Move each starred track of the discovery library to the keep folder, keeping its subfolders.

    Args:
        navidrome: The logged-in API.
        discovery_id: The discovery library's id.
        settings: The settings.
        scan_targets: The folders to rescan once a track moved, added to the pending scans before any file changes.

    Raises:
        RuntimeError: A track's path leads out of its folder, or a copy came out the wrong size.
        OSError: A move failed.
    """
    discovery, keep = settings.discovery_path, settings.keep_path
    songs = typing.cast(
        "list[Json]",
        navidrome.api(
            "GET",
            "/api/song",
            query=[("starred", "true"), ("library_id", str(discovery_id)), ("missing", "false"), ("_start", "0"), ("_end", str(PAGE_END))],
        ),
    )
    for song in songs:
        # Checked again: never move a file from another library
        if song.get("libraryId") != discovery_id or not song.get("starred"):
            continue
        relative = pathlib.Path(typing.cast("str", song["path"]))
        source = inside(discovery / relative, discovery)
        target = inside(keep / relative, keep)
        if not source.is_file():
            logger.warning("Starred but not on disk, skipped: %s", source)
            continue
        if target.exists():
            if target.stat().st_size != source.stat().st_size:
                logger.warning("Another file is already at %s: %s stays until the weekly run deletes it", target, source)
                continue
            add_pending_scans(settings.pending_scan_file, scan_targets)
            source.unlink()
            logger.info("Already kept, removed the discovery copy: %s", target)
            continue
        add_pending_scans(settings.pending_scan_file, scan_targets)
        move_file(source, target)
        logger.info("Kept %s", target)


def trash_listed(
    navidrome: Navidrome, playlist_name: str, folders: dict[int, pathlib.Path], trash: pathlib.Path, pending_scan: pathlib.Path
) -> None:
    """Move each track of the user's delete playlist to today's trash folder, then take them out of the playlist.

    A track that can't be moved stays in the playlist, for the next run.

    Args:
        navidrome: The logged-in API.
        playlist_name: The name of the user's delete playlist.
        folders: Each library's folder, by library id.
        trash: The trash folder.
        pending_scan: The pending scans file.

    Raises:
        RuntimeError: A track's path leads out of its library, or a copy came out the wrong size.
    """
    playlists = typing.cast("list[Json]", navidrome.api("GET", "/api/playlist", query=[("_start", "0"), ("_end", str(PAGE_END))]))
    # An admin sees every user's playlists: only the user's own counts
    own = [playlist for playlist in playlists if playlist.get("name") == playlist_name and playlist.get("ownerName") == navidrome.username]
    if not own:
        return
    playlist_id = str(own[0]["id"])
    # Each entry has its own id, for its place in the playlist, plus the track's fields
    entries = typing.cast(
        "list[Json]", navidrome.api("GET", f"/api/playlist/{playlist_id}/tracks", query=[("_start", "0"), ("_end", str(PAGE_END))])
    )
    day_folder = trash / today().isoformat()
    done: list[str] = []
    for entry in entries:
        library = folders.get(typing.cast("int", entry.get("libraryId")))
        if library is None:
            logger.warning("Track of an unknown library, left in the playlist: %s", entry.get("path"))
            continue
        source = inside(library / typing.cast("str", entry["path"]), library)
        if not source.is_file():
            logger.info("Not on disk, taken out of the playlist: %s", source)
            done.append(str(entry["id"]))
            continue
        target = day_folder / source.relative_to("/")
        if target.exists():
            logger.warning("Already in today's trash, left in the playlist: %s", target)
            continue
        add_pending_scans(pending_scan, [f"{entry['libraryId']}:{source.parent.relative_to(library.resolve()).as_posix()}"])
        try:
            move_file(source, target)
        except OSError:
            logger.exception("Couldn't trash %s, left in the playlist", source)
            continue
        logger.info("Trashed %s", source)
        done.append(str(entry["id"]))
    # All at once: Navidrome renumbers the entries after each removal
    if done:
        navidrome.api("DELETE", f"/api/playlist/{playlist_id}/tracks", query=[("id", entry_id) for entry_id in done])


def empty_folder(folder: pathlib.Path) -> None:
    """Delete everything inside the folder, keeping the folder itself.

    Args:
        folder: The folder to empty.
    """
    for child in folder.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    logger.info("Emptied %s", folder)


def is_scanning(navidrome: Navidrome) -> bool:
    """Whether Navidrome is scanning any library.

    Args:
        navidrome: The logged-in API.

    Returns:
        True while a scan runs.
    """
    return bool(typing.cast("Json", navidrome.subsonic("getScanStatus")["scanStatus"]).get("scanning"))


def wait_for_scan(navidrome: Navidrome, deadline: float, what: str) -> None:
    """Wait until Navidrome is not scanning.

    Args:
        navidrome: The logged-in API.
        deadline: A `time.monotonic()` value.
        what: What is waited for, for the error message.

    Raises:
        RuntimeError: Navidrome still scans past the deadline.
    """
    while is_scanning(navidrome):
        if time.monotonic() > deadline:
            msg = f"Navidrome still scans past the timeout, waiting for {what}"
            raise RuntimeError(msg)
        time.sleep(SCAN_POLL_SECONDS)


def scan(navidrome: Navidrome, targets: list[str], timeout_minutes: int) -> None:
    """Scan only these library folders and wait until Navidrome is done.

    A scan already running is waited out first: Navidrome doesn't start another one meanwhile.

    Args:
        navidrome: The logged-in API.
        targets: Folders as `<library id>:<folder in the library>`.
        timeout_minutes: How long to wait in all.

    Raises:
        RuntimeError: Navidrome refused the scan, or still scans past the timeout.
    """
    deadline = time.monotonic() + timeout_minutes * 60
    if is_scanning(navidrome):
        logger.info("Waiting for the scan Navidrome is already running")
        wait_for_scan(navidrome, deadline, "the scan already running")
    logger.info("Rescanning %s", ", ".join(targets))
    navidrome.subsonic("startScan", query=[("target", target) for target in targets])
    time.sleep(SCAN_POLL_SECONDS)
    wait_for_scan(navidrome, deadline, f"its scan of {', '.join(targets)}")
    logger.info("Rescanned %s", ", ".join(targets))


def scan_pending(navidrome: Navidrome, pending_scan: pathlib.Path, timeout_minutes: int) -> None:
    """Rescan the folders listed in the pending scans file, if any, then remove it.

    Args:
        navidrome: The logged-in API.
        pending_scan: The pending scans file.
        timeout_minutes: How long to wait for the scan.

    Raises:
        RuntimeError: Navidrome refused the scan, or still scans past the timeout.
    """
    if not pending_scan.exists():
        return
    targets = list(dict.fromkeys(line for line in pending_scan.read_text().splitlines() if line))
    if targets:
        scan(navidrome, targets, timeout_minutes)
    pending_scan.unlink()


def list_missing(navidrome: Navidrome, query: list[tuple[str, str]] | None = None) -> list[Json]:
    """Navidrome's missing tracks.

    Args:
        navidrome: The logged-in API.
        query: Extra filters, such as `library_id`.

    Returns:
        The missing tracks.
    """
    return typing.cast("list[Json]", navidrome.api("GET", "/api/missing", query=[*(query or []), ("_start", "0"), ("_end", str(PAGE_END))]))


def delete_missing(navidrome: Navidrome, ids: list[str]) -> None:
    """Remove these missing tracks from Navidrome.

    Args:
        navidrome: The logged-in API.
        ids: The tracks' ids; none, nothing happens.
    """
    # Without ids, Navidrome would remove every missing track
    if ids:
        navidrome.api("DELETE", "/api/missing", query=[("id", song_id) for song_id in ids])


def remove_missing(navidrome: Navidrome, discovery_id: int) -> None:
    """Remove the discovery library's missing tracks from Navidrome, leaving every other library's alone.

    Args:
        navidrome: The logged-in API.
        discovery_id: The discovery library's id.
    """
    missing = list_missing(navidrome, [("library_id", str(discovery_id))])
    # Filtered again: another library's missing files may only be its drive briefly down
    ids = [str(song["id"]) for song in missing if song.get("libraryId") == discovery_id]
    delete_missing(navidrome, ids)
    logger.info("Removed %d missing tracks from the discovery library", len(ids))


def empty_old_trash(navidrome: Navidrome, trash: pathlib.Path, days: int, folders: dict[int, pathlib.Path]) -> None:
    """Delete the trash's day folders older than `days`, after removing their tracks from Navidrome's missing files.

    Args:
        navidrome: The logged-in API.
        trash: The trash folder.
        days: How many days a trashed track waits.
        folders: Each library's folder, by library id.
    """
    if not trash.is_dir():
        return
    oldest_kept = today() - datetime.timedelta(days=days)
    for day_folder in sorted(trash.iterdir()):
        try:
            day = datetime.date.fromisoformat(day_folder.name)
        except ValueError:
            continue
        if day >= oldest_kept or not day_folder.is_dir():
            continue
        # Each file sits at its old path on the server, below the day folder
        old_paths = {pathlib.Path("/") / file.relative_to(day_folder) for file in day_folder.rglob("*") if file.is_file()}
        ids = [
            str(song["id"])
            for song in list_missing(navidrome)
            if (library := folders.get(typing.cast("int", song.get("libraryId")))) is not None
            and (library / typing.cast("str", song["path"])).resolve() in old_paths
        ]
        delete_missing(navidrome, ids)
        shutil.rmtree(day_folder)
        logger.info(
            "Deleted the trash of %s: %d files, %d removed from Navidrome's missing files", day_folder.name, len(old_paths), len(ids)
        )


def is_service_idle(service: str) -> bool:
    """Whether the systemd service is neither running nor starting.

    Args:
        service: The service, such as `explo.service`.

    Returns:
        True when it is inactive or failed.
    """
    state = subprocess.run(  # noqa: S603 (fixed command, service name from the settings)
        [SYSTEMCTL, "show", "--property=ActiveState", "--value", service], capture_output=True, text=True, check=True
    ).stdout.strip()
    return state in IDLE_SERVICE_STATES


def lock(lock_file: pathlib.Path, *, wait: bool) -> typing.TextIO | None:
    """Take the lock all runs share.

    Args:
        lock_file: The lock file.
        wait: Wait for another run to finish, rather than give up.

    Returns:
        The lock file, open, or None if another run has it and `wait` is False.
    """
    handle = lock_file.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def check_folders(settings: Settings, folders: dict[int, pathlib.Path]) -> None:
    """Refuse settings that could delete the wrong files.

    Args:
        settings: The settings.
        folders: Each library's folder, by library id.

    Raises:
        RuntimeError: The discovery folder is missing, the keep folder is outside the main library, or the trash is
            inside a library.
    """
    if not settings.discovery_path.is_dir() or not settings.keep_path.is_relative_to(settings.main_library_path):
        msg = f"{settings.discovery_path} must be a folder, and {settings.keep_path} inside {settings.main_library_path}"
        raise RuntimeError(msg)
    trash = settings.trash_path
    if trash is not None and any(trash.is_relative_to(folder) or folder.is_relative_to(trash) for folder in folders.values()):
        msg = f"The trash {trash} must be outside every Navidrome library"
        raise RuntimeError(msg)


def upkeep(settings: Settings, *, frequent: bool) -> None:
    """Run every step, or with `frequent`, every step but the weekly ones.

    Args:
        settings: The settings.
        frequent: Run as the frequent mode: skip the weekly steps, and do nothing while the weekly service runs, another
            run holds the lock or Navidrome scans.
    """
    # A frequent run never runs during the weekly run, Explo included
    if frequent and not is_service_idle(settings.weekly_service):
        return
    # The weekly run waits for a frequent run to finish; a frequent run gives up if the lock is taken
    held = lock(settings.lock_file, wait=not frequent)
    if held is None:
        return
    with held:
        username, password = read_login(settings.login_file)
        navidrome = Navidrome(settings.navidrome_url, username, password)
        # Navidrome may still be taking in a moved or new file: leave it to the next run
        if frequent and is_scanning(navidrome):
            return
        folders = library_folders(navidrome, settings)
        check_folders(settings, folders)
        discovery_id = library_id(folders, settings.discovery_path)
        main_id = library_id(folders, settings.main_library_path)

        keep_targets = [f"{discovery_id}:", f"{main_id}:{settings.keep_path.relative_to(settings.main_library_path).as_posix()}"]
        keep_starred(navidrome, discovery_id, settings, keep_targets)
        if settings.delete_playlist is not None and settings.trash_path is not None:
            trash_listed(navidrome, settings.delete_playlist, folders, settings.trash_path, settings.pending_scan_file)
        if not frequent:
            add_pending_scans(settings.pending_scan_file, [f"{discovery_id}:"])
            empty_folder(settings.discovery_path)
        scan_pending(navidrome, settings.pending_scan_file, settings.scan_timeout_minutes)
        if not frequent:
            remove_missing(navidrome, discovery_id)
        if settings.trash_path is not None:
            empty_old_trash(navidrome, settings.trash_path, settings.trash_days, folders)


def main() -> None:
    """Parse the command line and run."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("settings", type=pathlib.Path, help="TOML settings file")
    parser.add_argument("--frequent", action="store_true", help="skip the weekly steps, and do nothing while the weekly service runs")
    arguments = parser.parse_args()
    upkeep(Settings.load(typing.cast("pathlib.Path", arguments.settings)), frequent=typing.cast("bool", arguments.frequent))


if __name__ == "__main__":
    main()
