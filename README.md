# Midas Companion

Source code of **Midas Companion** 1.3.1, the Windows companion app for the World of Warcraft addon
**[Midas](https://www.curseforge.com/wow/addons/midas)** and the website **[midasmarket.app](https://midasmarket.app)**.

This is the code the Microsoft Store app is built from. It is published so that everyone can read what
the app does before running it.

**Download:** [Microsoft Store](https://apps.microsoft.com/detail/9MTS01S8BQR0) or
[midasmarket.app/download](https://midasmarket.app/download)

---

## What the app does

Game addons cannot connect to the internet. Midas Companion is the link between Midas and midasmarket.app:

| | |
|---|---|
| **Sharing scans** (opt-in) | After a full Midas scan, Midas stores the offer book in its SavedVariables file. After the next `/reload` or logout the app reads it and sends it to midasmarket.app, where the scans of all participants become current prices, price history and the offer book of each market. |
| **Price scans** (opt-in) | With Midas 1.3.1, every complete price scan between full scans (lowest price and number of listings per item) is sent as well, so the prices on midasmarket.app stay current. Before the first price scan the app asks the server (`/api/v1/health`) whether it accepts them. |
| **Item base data** (opt-in) | With a shared scan the app sends once per item the base data Midas has read in the game (icon, item class and subclass, binding, item level, required level, slot, stack size, vendor price). It is only marked as sent once the server confirms it. |
| **Server prices** | The app downloads the server prices of the markets you play on (up to 90 days) and writes them to `Prices.lua` in the Midas addon folder. Midas uses them after the next login or `/reload`. This works without sharing your scans. |
| **Price history** (opt-in) | With sharing on, the app sends once per market the price history Midas has kept on your PC: per item and day the lowest price and the quantity of the last up to 90 days. |

Which markets get server prices:

- markets Midas has already seen in the game
- realms detected from the folder names in `WTF\Account\...\<realm>\<character>` (changed in the last 30 days), with the region from `WTF\Config.wtf`, so a new player has prices already at the first login
- markets added by hand in the app window ("Add market": game version, region, realm, faction)
- for WoW Classic Forever, whose realm folders are numbers, the list of known Forever markets from the server (`/api/v1/markets`)

The app runs in the notification area and has a window with a setup checklist, your markets, a live log
and the settings. It does not interact with the running game.

## What is sent, and what is not

Sharing is **off** until you turn it on in the game: `/midas datalink on`, or "Share my scans" under
DATALINK in Midas. `/midas datalink off` turns it off again.

**With sharing on**, an upload contains:

- the market: game version, region, realm, faction, ruleset
- scan time, item IDs, prices and quantities (the offer book; for a price scan only lowest price and number of listings)
- item names and quality in your game language, once per item
- item base data as listed above, once per item
- once per market the price history of the last up to 90 days (it roughly shows on which days you scanned)
- the app version and a random installation ID (`client_id`) with no connection to your Battle.net account

**Loading server prices** (also with sharing off) sends only the market fields (game version, region,
realm, faction, ruleset) and the app version. Realm names from the game folder and markets added by hand
are only used for these requests.

**Never sent:** character, account, guild or seller names, gold, bags, or anything else from your PC.

As with every internet connection the server sees your IP address. How the server stores data (hashed
`client_id` and IP address, retention periods) is described in the
[privacy policy](https://midasmarket.app/#/datenschutz). The installation ID is shown in the app window;
send it to support@midasmarket.app for access or deletion requests.

The only host the app talks to is `midasmarket.app` (`/api/v1/snapshots`, `/api/v1/history`,
`/api/v1/export`, `/api/v1/markets`, `/api/v1/health`). Redirects are never followed. There is no telemetry and no update check.

## What it reads and writes on your PC

| Where | What |
|---|---|
| `<WoW>\<flavor>\WTF\Account\*\SavedVariables\Midas.lua` (and the older `Midas_Uplink.lua`) | read only; parsed as data, nothing is executed |
| `<WoW>\<flavor>\WTF\Account\*\<realm>\<character>` folder names and `<WoW>\<flavor>\WTF\Config.wtf` (region) | read only; only names and change times of folders, no file contents except the region line |
| `<WoW>\<flavor>\Interface\AddOns\Midas\Prices.lua` (and `AddOns\Midas_Uplink\Prices.lua` while that old addon exists) | the only file written in the game folder |
| `%APPDATA%\MidasUplink\` | `state.json` (installation ID, what was already sent, settings), `uploader.log` (rotating), `prices_cache\` |
| `%LOCALAPPDATA%\Packages\<package>\LocalState\MidasUplink\` | the same files for the Microsoft Store app |
| `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Midas Uplink.lnk` | only when "Start with Windows" is on (EXE; the Store app uses a Windows startup task) |

Flavors: `_classic_era_`, `_forever_`, `_anniversary_`.

## The app window

The window is a local page served by the app itself (`ui_server.py`, `ui\`):

- bound to `127.0.0.1` only, on a random port, never reachable from the network
- everything lives under a random secret path; any other path answers 404
- the window is opened with a one-time ticket (valid 120 seconds, usable once) that is exchanged for a
  session cookie (HttpOnly, SameSite=Strict, limited to the secret path)
- actions additionally need a page token that exists only inside the page, plus Host and Origin checks
- strict Content-Security-Policy, no external resources, no CORS

## Run from source

Windows 10/11 and Python 3.12.

```
pip install -r requirements.txt
python tray.py
```

To see exactly what would be sent, without contacting the server:

```
python uploader.py --once --outbox C:\Temp\midas-outbox
```

This writes each upload as a JSON file into the folder instead of sending it.

Other options:

| Command | Does |
|---|---|
| `python uploader.py` | watches in the console, checks every 30 seconds |
| `python uploader.py --once` | one pass, then exit |
| `python uploader.py --prices-now` | loads server prices once, then exit |
| `--wow <dir>` | additional game folder (besides auto-detection) |
| `--only-wow <dir>` | only this game folder, no auto-detection |
| `python tray.py --no-window` | starts in the notification area without opening the window |

`uploader.py`, `ui_server.py` and `package_env.py` use only the Python standard library; `tray.py` needs
`pystray` and `Pillow`.

## Build it yourself

```
pip install -r requirements.txt pyinstaller
python build.py
```

The result is `dist\MidasUplink.exe`. A self-built EXE is not byte-identical to the released one
(PyInstaller builds are not reproducible), and it is not code-signed.

`msix\AppxManifest.xml` is the manifest template of the Microsoft Store package (identity fields are
placeholders). The Store package contains the same Python code, built with PyInstaller as a folder.

## Tests

```
pip install pytest lupa
python -m pytest tests -q
```

Every test uses its own `APPDATA` folder and may only open loopback addresses, so the real state and the
real server are never touched. `lupa` (Lua 5.1) is only needed for the `Prices.lua` syntax tests; without
it they are skipped.

## Files

| File | What |
|---|---|
| `uploader.py` | reading SavedVariables, uploads, server prices, state; also the command line |
| `tray.py` | notification-area app, autostart, starts the window |
| `ui_server.py`, `ui\` | the local app window |
| `package_env.py` | detection of the Store package and its startup task |
| `build.py` | PyInstaller build |
| `tests\` | pytest suite |

## License

MIT, Copyright (c) 2026 Rebo.

Midas Market is a fan project, not affiliated with Blizzard Entertainment. World of Warcraft is a
trademark of Blizzard Entertainment, Inc.
