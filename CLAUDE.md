# navidrome-upkeep: agent guide

- The script runs on a server's own Python (3.13 or later): standard library only, in one file, so a deployment fetches just `navidrome_upkeep.py` at a pinned commit. Adding a dependency or splitting the file changes how it is deployed: don't, without asking.
- It moves and deletes music files. Any change to what it moves, deletes or removes from Navidrome comes with a test in `tests/`, against the fake Navidrome there. The tests need Linux: on Windows, run them in WSL or a container.
- The Navidrome API calls were checked against Navidrome's source (0.64): its native API (`/api/...`) is undocumented, so check a changed call against the source of a tagged release, not against memory.
- A call to `DELETE /api/missing` without ids makes Navidrome remove every missing track of every library: `delete_missing` guards it, and the fake Navidrome fails a test that sends one.
