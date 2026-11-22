# navidrome-upkeep

*This project is vibe-coded: written by Claude, Anthropic's AI.*

A companion script for [Explo](https://github.com/LumePart/Explo) and [Navidrome](https://www.navidrome.org/). Explo downloads a weekly discovery playlist into a Navidrome library of its own and never deletes anything. This script:

- **keeps what you star**: every few minutes, it moves the discovery tracks you starred to a folder of your main library (a "to sort" folder, say), then removes its star: the star only marks what to keep, it isn't a favourite;
- **empties the discovery library** before each weekly run, once the starred tracks are safe;
- **deletes from any player** (optional): add a track to a playlist of yours, such as `delete`, and within minutes it moves to a dated trash folder outside your libraries, where it stays 30 days (or what you set) before it's deleted for good.

Written against Navidrome 0.64, with the discovery folder added as a second library. Standard library only: Python 3.13 or later, on Linux (it uses `fcntl` and `systemctl`).

## How it works

Run with its settings file as argument, in one of two modes:

- **weekly** (`navidrome_upkeep.py settings.toml`), just before Explo, for example as the Explo service's `ExecStartPre`: keeps the starred tracks, trashes the delete playlist's, empties the discovery folder, rescans, then removes the deleted tracks from Navidrome's missing files. If anything fails, it stops before deleting anything, and the service fails too.
- **frequent** (`navidrome_upkeep.py --frequent settings.toml`), every few minutes from a systemd timer: everything but emptying the discovery folder. It does nothing while the weekly service runs, while another run holds the lock, or while Navidrome scans; the next run catches up.

Navidrome only notices a moved file when it rescans. The script rescans only the folders it changed, never a whole library. A track moved between libraries within one scan stays the same track for Navidrome, with its play counts, playlist entries and star.

**Keeping**: the track moves from `<discovery_path>/Weekly-Exploration/x.flac` to `<keep_path>/Weekly-Exploration/x.flac`, then loses its star. If removing the star fails, the track is kept all the same, still starred, and the log says so. A track starred by mistake moves all the same. Navidrome recognises a moved track by its tags: a kept track retagged before the rescan is seen as a new track, and the old one stays in Settings > Missing Files.

**Deleting**: the track moves to `<trash_path>/<date>/<its path on the server>`, for example `<trash_path>/2026-11-20/mnt/music/Artist/Album/x.flac`, and leaves the playlist. Navidrome then shows it as missing, keeping its stars and play counts. To bring it back, move it back to its old path: the next scan finds it with its history. After `trash_days`, the day's folder is deleted, and only then are its tracks removed from Navidrome's missing files. This needs Navidrome to keep missing files, which is its default (`Scanner.PurgeMissing = "never"`): with `always`, every scan would forget them at once. Album folders left empty stay where they are.

## Settings

A TOML file. Every key is required, except `delete_playlist` and `trash_path`, which go together.

```toml
# Navidrome's address
navidrome_url = "http://127.0.0.1:4533"
# Explo's env file: the script logs in with its SYSTEM_USERNAME and SYSTEM_PASSWORD. Stars and playlists are per
# user, and that user must be a Navidrome admin (the script lists libraries, starts scans and removes missing files)
login_file = "/srv/explo/env"
# Explo's download folder, added to Navidrome as a library of its own
discovery_path = "/srv/explo/discovery"
# Where starred tracks go: a folder inside the main library
keep_path = "/mnt/music/_to-sort"
# The main library, as this script sees it, and as Navidrome sees it (its path in Navidrome's container, say)
main_library_path = "/mnt/music"
main_library_navidrome_path = "/music"
# How long a run waits for Navidrome's rescan before failing
scan_timeout_minutes = 60
# The systemd service that runs Explo, which the frequent mode waits out
weekly_service = "explo.service"
# Files of the script's own, writable by the user it runs as
lock_file = "/srv/explo/navidrome-upkeep.lock"
pending_scan_file = "/srv/explo/navidrome-upkeep.pending-scan"
# Days a deleted track waits in the trash
trash_days = 30
# Deleting (optional): the user's playlist, and the trash folder, outside every Navidrome library. The trash is best
# on the same drive as the main library, so that a move is a rename
delete_playlist = "delete"
trash_path = "/mnt/music-trash"
```

Every library other than the main one must be at the same path for the script and for Navidrome.

## Development

```sh
uv sync
uv run ruff check
uv run ruff format --check
uv run basedpyright
uv run pytest
```

The tests run the script against a fake Navidrome and a fake `systemctl`, on temporary folders, so they need Linux (or WSL). CI runs all of the above on each push.
