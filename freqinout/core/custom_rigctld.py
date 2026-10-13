"""Explicit native rigctld command identity; no shell or script evaluation."""
from dataclasses import dataclass
from pathlib import Path
import shlex

@dataclass(frozen=True)
class CustomRigctld:
    executable: str
    arguments: tuple[str, ...]
    host: str
    port: int


def parse_custom_rigctld(command: str):
    try:
        parts = shlex.split(str(command or ""))
    except ValueError:
        return None
    if not parts or Path(parts[0]).name.lower() not in {"rigctld", "rigctld.exe"}:
        return None
    # Native argv only. Shell wrappers, expansions and pipelines have no
    # durable process identity and are intentionally not interpreted here.
    if any(any(mark in part for mark in ("$", "`", ";", "|", "&", "<", ">")) for part in parts):
        return None
    values = {}
    for option in ("-m", "-r", "-T", "-t"):
        matches = [(i, part) for i, part in enumerate(parts[1:], 1)
                   if part == option or part.startswith(option) and len(part) > len(option)]
        if len(matches) != 1:
            return None
        index, token = matches[0]
        if token == option:
            if index + 1 >= len(parts):
                return None
            values[option] = parts[index + 1]
        else:
            values[option] = token[len(option):]
    try:
        port = int(values["-t"])
        upstream_host, upstream_port = values["-r"].rsplit(":", 1)
        if values["-m"] != "4" or not upstream_host or not 0 < int(upstream_port) < 65536:
            return None
        if not values["-T"] or not 0 < port < 65536:
            return None
    except (ValueError, KeyError):
        return None
    return CustomRigctld(parts[0], tuple(parts[1:]), values["-T"], port)
