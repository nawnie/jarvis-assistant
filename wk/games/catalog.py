"""Which programs are games, what kind, and where their logs and settings live.

The catalog is deliberately small and hand-written: Bethesda games and emulators are the two
things Shawn fixes most, so those carry the most detail (My Games folder, script extender,
where crash loggers write). It exists for game HELP (knowing where to look); autoplay does not
consult it for permission - which games to automate is up to Shawn and each game's makers.
"""
import os
from dataclasses import dataclass, field

import psutil


@dataclass(frozen=True)
class Game:
    name: str
    family: str                  # "bethesda" | "emulator" | "other"
    steam_id: int | None = None
    my_games: str = ""           # folder under Documents\My Games (Bethesda ini files and logs)
    appdata: str = ""            # folder under %LOCALAPPDATA% (plugins.txt / load order)
    extender: str = ""           # script extender folder name under My Games\<game> (SKSE, F4SE...)
    tips: tuple = field(default_factory=tuple)   # known shortcuts and fixes worth offering


# ---------------------------------------------------------------------------
# this is the Bethesda section (exe name -> game)
# ---------------------------------------------------------------------------
_SKYRIM_TIPS = (
    "Skip the Helgen intro with the Alternate Start - Live Another Life mod (cleanest for a real playthrough). "
    "Quick and dirty: at the main menu open the console (~), type `coc riverwood`, then `showracemenu` to "
    "make your character - this skips the Unbound quest entirely.",
    "Crash logs: install Crash Logger SSE AE VR (SKSE plugin); it writes crash-*.log under "
    "Documents\\My Games\\Skyrim Special Edition\\SKSE.",
)
BETHESDA = {
    "skyrimse.exe": Game("Skyrim Special Edition", "bethesda", 489830, "Skyrim Special Edition",
                         "Skyrim Special Edition", "SKSE", tips=_SKYRIM_TIPS),
    "skyrim.exe": Game("Skyrim (Legendary)", "bethesda", 72850, "Skyrim", "Skyrim", "SKSE"),
    "skyrimvr.exe": Game("Skyrim VR", "bethesda", 611670, "Skyrim VR", "Skyrim VR", "SKSE"),
    "fallout4.exe": Game("Fallout 4", "bethesda", 377160, "Fallout4", "Fallout4", "F4SE",
                         tips=("Crash logs: Buffout 4 writes crash-*.log under Documents\\My Games\\Fallout4\\F4SE.",)),
    "starfield.exe": Game("Starfield", "bethesda", 1716740, "Starfield", "Starfield", "SFSE"),
    "falloutnv.exe": Game("Fallout: New Vegas", "bethesda", 22380, "FalloutNV", "FalloutNV", "NVSE"),
    "fallout3.exe": Game("Fallout 3", "bethesda", 22300, "Fallout3", "Fallout3", "FOSE"),
    "oblivion.exe": Game("Oblivion", "bethesda", 22330, "Oblivion", "Oblivion", "OBSE"),
    "fallout76.exe": Game("Fallout 76", "bethesda", 1151340, "Fallout 76"),
}

# ---------------------------------------------------------------------------
# this is the emulator section. Emulator exe names vary by build ("pcsx2-qt.exe",
# "duckstation-qt-x64-ReleaseLTCG.exe"), so these match by name PREFIX.
# ---------------------------------------------------------------------------
EMULATORS = {
    "project64": Game("Project64 (N64)", "emulator"),
    "retroarch": Game("RetroArch", "emulator"),
    "dolphin": Game("Dolphin (GameCube/Wii)", "emulator"),
    "pcsx2": Game("PCSX2 (PS2)", "emulator"),
    "duckstation": Game("DuckStation (PS1)", "emulator"),
    "rpcs3": Game("RPCS3 (PS3)", "emulator"),
    "ppsspp": Game("PPSSPP (PSP)", "emulator"),
    "cemu": Game("Cemu (Wii U)", "emulator"),
    "ryujinx": Game("Ryujinx (Switch)", "emulator"),
    "yuzu": Game("yuzu (Switch)", "emulator"),
    "suyu": Game("suyu (Switch)", "emulator"),
    "sudachi": Game("Sudachi (Switch)", "emulator"),
    "citron": Game("Citron (Switch)", "emulator"),
    "xenia": Game("Xenia (Xbox 360)", "emulator"),
    "melonds": Game("melonDS (DS)", "emulator"),
    "mgba": Game("mGBA (GBA)", "emulator"),
}

# a few other games seen on this PC
OTHER = {
    "hogwartslegacy.exe": Game("Hogwarts Legacy", "other", 990080),
    "gta5_enhanced.exe": Game("GTA V Enhanced", "other", 3240220),
    "eso64.exe": Game("The Elder Scrolls Online", "other", 306130),
}


def detect(process_name):
    """Game for a process name ('skyrimse.exe'), or None if it isn't a game Jarvis knows."""
    name = (process_name or "").lower()
    if name in BETHESDA:
        return BETHESDA[name]
    if name in OTHER:
        return OTHER[name]
    for prefix, game in EMULATORS.items():
        if name.startswith(prefix):
            return game
    return None


def running_games():
    """[(process name, pid, Game)] for every known game running right now."""
    found = []
    for proc in psutil.process_iter(["pid", "name"]):
        game = detect(proc.info["name"])
        if game:
            found.append(((proc.info["name"] or "").lower(), proc.info["pid"], game))
    return found


def documents_dir():
    from ..config import known_folder
    return known_folder("FDD39AD0-238F-46AF-ADB4-6C85480369C7", os.path.join(os.environ.get("USERPROFILE", ""), "Documents"))
