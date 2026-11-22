"""Tests against a fake Navidrome and a fake systemctl, on temporary folders. POSIX only, like the script (fcntl)."""

import collections.abc
import dataclasses
import datetime
import fcntl
import http.server
import json
import pathlib
import threading
import urllib.parse

import pytest

import navidrome_upkeep

USERNAME = "user"
MAIN_NAVIDROME_PATH = "/music"
PLAYLIST_ID = "P1"


@dataclasses.dataclass
class FakeState:
    """What the fake Navidrome serves and records."""

    discovery_path: str
    starred: list[str] = dataclasses.field(default_factory=list)
    scanning: bool = False
    entries: list[dict[str, object]] = dataclasses.field(default_factory=list)
    missing: list[dict[str, object]] = dataclasses.field(default_factory=list)
    # Recorded calls
    scans: list[list[str]] = dataclasses.field(default_factory=list)
    deleted_missing: list[str] = dataclasses.field(default_factory=list)
    unlisted: list[str] = dataclasses.field(default_factory=list)
    unstarred: list[str] = dataclasses.field(default_factory=list)
    unstar_fails: bool = False
    calls: int = 0


def make_handler(state: FakeState) -> type[http.server.BaseHTTPRequestHandler]:
    """A request handler serving the parts of Navidrome's API the script uses."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 (the base class's name)
            pass

        def send(self, answer: object) -> None:
            body = json.dumps(answer).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self) -> None:
            state.calls += 1
            self.send({"token": "token"})

        def do_DELETE(self) -> None:
            state.calls += 1
            url = urllib.parse.urlparse(self.path)
            ids = urllib.parse.parse_qs(url.query).get("id", [])
            if url.path == f"/api/playlist/{PLAYLIST_ID}/tracks":
                state.unlisted += ids
                state.entries = [entry for entry in state.entries if entry["id"] not in ids]
            else:
                # Without ids, the real Navidrome would remove every missing track
                assert url.path == "/api/missing", url.path
                assert ids, "DELETE /api/missing without ids"
                state.deleted_missing += ids
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:
            state.calls += 1
            url = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(url.query)
            answers: dict[str, object] = {
                "/api/library": [{"id": 1, "path": MAIN_NAVIDROME_PATH}, {"id": 2, "path": state.discovery_path}],
                "/api/song": [{"id": f"song:{path}", "libraryId": 2, "starred": True, "path": path} for path in state.starred],
                "/api/missing": state.missing,
                "/api/playlist": [
                    {"id": "P0", "name": "delete", "ownerName": "someone else"},
                    {"id": PLAYLIST_ID, "name": "delete", "ownerName": USERNAME},
                ],
                f"/api/playlist/{PLAYLIST_ID}/tracks": state.entries,
                "/rest/getScanStatus": {"subsonic-response": {"status": "ok", "scanStatus": {"scanning": state.scanning}}},
                "/rest/startScan": {"subsonic-response": {"status": "ok"}},
            }
            if url.path == "/rest/startScan":
                state.scans.append(query.get("target", []))
            if url.path == "/rest/unstar":
                if state.unstar_fails:
                    self.send({"subsonic-response": {"status": "failed", "error": {"code": 0, "message": "no"}}})
                    return
                state.unstarred += query.get("id", [])
                self.send({"subsonic-response": {"status": "ok"}})
                return
            if url.path not in answers:
                self.send_response(404)
                self.end_headers()
                return
            self.send(answers[url.path])

    return Handler


@dataclasses.dataclass
class Env:
    """A temporary setup: folders, settings, the fake Navidrome's state and the weekly service's state."""

    root: pathlib.Path
    discovery: pathlib.Path
    main: pathlib.Path
    keep: pathlib.Path
    trash: pathlib.Path
    settings_file: pathlib.Path
    service_state: pathlib.Path
    state: FakeState

    def settings(self) -> navidrome_upkeep.Settings:
        return navidrome_upkeep.Settings.load(self.settings_file)

    def run(self, *, frequent: bool) -> None:
        navidrome_upkeep.upkeep(self.settings(), frequent=frequent)


@pytest.fixture
def env(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> collections.abc.Iterator[Env]:
    """Discovery and main libraries with three discovery tracks, a fake Navidrome, a fake systemctl."""
    discovery = tmp_path / "discovery"
    main = tmp_path / "main"
    keep = main / "_to-sort"
    for folder in (discovery / "Weekly", keep):
        folder.mkdir(parents=True)
    for name in "abc":
        (discovery / "Weekly" / f"{name}.flac").write_bytes(name.encode() * 100)
    login = tmp_path / "env"
    login.write_text(f"{navidrome_upkeep.USERNAME_KEY}={USERNAME}\n{navidrome_upkeep.PASSWORD_KEY}=password\n")

    state = FakeState(discovery_path=str(discovery))
    server = http.server.HTTPServer(("127.0.0.1", 0), make_handler(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()

    service_state = tmp_path / "service-state"
    service_state.write_text("inactive")
    systemctl = tmp_path / "systemctl"
    systemctl.write_text(f"#!/bin/sh\ncat {service_state}\n")
    systemctl.chmod(0o755)
    monkeypatch.setattr(navidrome_upkeep, "SYSTEMCTL", str(systemctl))
    monkeypatch.setattr(navidrome_upkeep, "SCAN_POLL_SECONDS", 0.01)

    trash = tmp_path / "trash"
    settings_file = tmp_path / "settings.toml"
    settings_file.write_text(
        f'navidrome_url = "http://127.0.0.1:{server.server_port}"\n'
        f'login_file = "{login}"\n'
        f'discovery_path = "{discovery}"\n'
        f'keep_path = "{keep}"\n'
        f'main_library_path = "{main}"\n'
        f'main_library_navidrome_path = "{MAIN_NAVIDROME_PATH}"\n'
        "scan_timeout_minutes = 1\n"
        'weekly_service = "explo.service"\n'
        f'lock_file = "{tmp_path / "lock"}"\n'
        f'pending_scan_file = "{tmp_path / "pending"}"\n'
        "trash_days = 30\n"
        'delete_playlist = "delete"\n'
        f'trash_path = "{trash}"\n'
    )
    yield Env(tmp_path, discovery, main, keep, trash, settings_file, service_state, state)
    server.shutdown()


def test_frequent_waits_out_the_weekly_service(env: Env) -> None:
    env.state.starred = ["Weekly/a.flac"]
    env.service_state.write_text("activating")
    env.run(frequent=True)
    assert (env.discovery / "Weekly/a.flac").exists()
    assert env.state.calls == 0


def test_frequent_waits_out_a_scan(env: Env) -> None:
    env.state.starred = ["Weekly/a.flac"]
    env.state.scanning = True
    env.run(frequent=True)
    assert (env.discovery / "Weekly/a.flac").exists()
    assert not env.state.scans


def test_frequent_gives_up_while_another_run_holds_the_lock(env: Env) -> None:
    env.state.starred = ["Weekly/a.flac"]
    with (env.root / "lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        env.run(frequent=True)
    assert env.state.calls == 0


def test_frequent_keeps_a_starred_track(env: Env) -> None:
    env.state.starred = ["Weekly/a.flac"]
    env.run(frequent=True)
    assert (env.keep / "Weekly/a.flac").read_bytes() == b"a" * 100
    assert not (env.discovery / "Weekly/a.flac").exists()
    assert (env.discovery / "Weekly/b.flac").exists()
    assert env.state.scans == [["2:", "1:_to-sort"]]
    assert not env.state.deleted_missing
    assert not (env.root / "pending").exists()


def test_a_kept_track_loses_its_star(env: Env) -> None:
    env.state.starred = ["Weekly/a.flac"]
    env.run(frequent=True)
    assert (env.keep / "Weekly/a.flac").exists()
    assert env.state.unstarred == ["song:Weekly/a.flac"]


def test_a_failed_move_keeps_the_star(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(source: pathlib.Path, target: pathlib.Path) -> None:
        raise OSError(source, target)

    monkeypatch.setattr(navidrome_upkeep, "move_file", fail)
    env.state.starred = ["Weekly/a.flac"]
    with pytest.raises(OSError, match=r"a[.]flac"):
        env.run(frequent=True)
    assert (env.discovery / "Weekly/a.flac").exists()
    assert not env.state.unstarred


def test_a_failed_unstar_still_keeps_the_track(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    env.state.starred = ["Weekly/a.flac"]
    env.state.unstar_fails = True
    env.run(frequent=True)
    assert (env.keep / "Weekly/a.flac").exists()
    assert "couldn't remove its star" in caplog.text


def test_frequent_with_nothing_to_do_does_not_scan(env: Env) -> None:
    env.run(frequent=True)
    assert not env.state.scans


def test_a_pending_scan_left_by_a_failed_run_is_done(env: Env) -> None:
    (env.root / "pending").write_text("2:\n2:\n")
    env.run(frequent=True)
    assert env.state.scans == [["2:"]]
    assert not (env.root / "pending").exists()


def test_already_kept_tracks(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    (env.keep / "Weekly").mkdir()
    (env.keep / "Weekly/b.flac").write_bytes(b"x" * 100)
    (env.keep / "Weekly/c.flac").write_bytes(b"x" * 5)
    env.state.starred = ["Weekly/b.flac", "Weekly/c.flac"]
    env.run(frequent=True)
    # Same size: the discovery copy goes
    assert not (env.discovery / "Weekly/b.flac").exists()
    # Another file: both stay, with a warning
    assert (env.discovery / "Weekly/c.flac").exists()
    assert (env.keep / "Weekly/c.flac").read_bytes() == b"x" * 5
    assert "Another file is already at" in caplog.text
    assert env.state.unstarred == ["song:Weekly/b.flac"]


def test_a_path_leading_out_of_the_library_stops_before_deleting(env: Env) -> None:
    env.state.starred = ["../../etc/passwd"]
    with pytest.raises(RuntimeError, match="is outside"):
        env.run(frequent=False)
    assert (env.discovery / "Weekly/c.flac").exists()


def test_weekly_empties_discovery_and_removes_its_missing_tracks_only(env: Env) -> None:
    env.state.missing = [{"id": "m1", "libraryId": 2, "path": "x"}, {"id": "m9", "libraryId": 1, "path": "y"}]
    env.run(frequent=False)
    assert list(env.discovery.iterdir()) == []
    assert env.state.scans == [["2:"]]
    assert env.state.deleted_missing == ["m1"]


def test_delete_playlist(env: Env) -> None:
    (env.main / "Artist/Album").mkdir(parents=True)
    (env.main / "Artist/Album/x.flac").write_bytes(b"x" * 10)
    env.state.entries = [
        {"id": "e1", "mediaFileId": "f1", "libraryId": 1, "path": "Artist/Album/x.flac"},
        {"id": "e2", "mediaFileId": "f2", "libraryId": 1, "path": "Artist/Album/gone.flac"},
        {"id": "e3", "mediaFileId": "f3", "libraryId": 7, "path": "z.flac"},
    ]
    env.run(frequent=True)
    trashed = env.trash / navidrome_upkeep.today().isoformat() / (env.main / "Artist/Album/x.flac").resolve().relative_to("/")
    assert trashed.read_bytes() == b"x" * 10
    assert not (env.main / "Artist/Album/x.flac").exists()
    # A track already gone is only taken out of the playlist; one of an unknown library stays in it
    assert sorted(env.state.unlisted) == ["e1", "e2"]
    assert [entry["id"] for entry in env.state.entries] == ["e3"]
    assert env.state.scans == [["1:Artist/Album"]]


def test_old_trash_is_deleted_with_its_missing_tracks_only(env: Env) -> None:
    old_day = (navidrome_upkeep.today() - datetime.timedelta(days=31)).isoformat()
    recent_day = navidrome_upkeep.today().isoformat()
    old_file = env.trash / old_day / (env.main / "Old/o.flac").resolve().relative_to("/")
    recent_file = env.trash / recent_day / (env.main / "New/n.flac").resolve().relative_to("/")
    for file in (old_file, recent_file):
        file.parent.mkdir(parents=True)
        file.write_bytes(b"o")
    (env.trash / "notes").mkdir()
    env.state.missing = [
        {"id": "mo", "libraryId": 1, "path": "Old/o.flac"},
        {"id": "mn", "libraryId": 1, "path": "New/n.flac"},
        {"id": "mz", "libraryId": 2, "path": "Old/o.flac"},
    ]
    env.run(frequent=True)
    assert env.state.deleted_missing == ["mo"]
    assert not (env.trash / old_day).exists()
    assert recent_file.exists()
    assert (env.trash / "notes").exists()


def test_trash_inside_a_library_is_refused(env: Env) -> None:
    env.settings_file.write_text(
        env.settings_file.read_text().replace(f'trash_path = "{env.trash}"', f'trash_path = "{env.main / "trash"}"')
    )
    with pytest.raises(RuntimeError, match="outside every Navidrome library"):
        env.run(frequent=True)


def test_delete_playlist_and_trash_go_together(env: Env) -> None:
    env.settings_file.write_text(env.settings_file.read_text().replace('delete_playlist = "delete"\n', ""))
    with pytest.raises(ValueError, match="set both"):
        env.settings()


def test_without_a_delete_playlist_nothing_is_trashed(env: Env) -> None:
    text = env.settings_file.read_text().replace('delete_playlist = "delete"\n', "").replace(f'trash_path = "{env.trash}"\n', "")
    env.settings_file.write_text(text)
    (env.main / "Artist").mkdir()
    (env.main / "Artist/x.flac").write_bytes(b"x")
    env.state.entries = [{"id": "e1", "mediaFileId": "f1", "libraryId": 1, "path": "Artist/x.flac"}]
    env.run(frequent=True)
    assert (env.main / "Artist/x.flac").exists()
    assert not env.state.unlisted
