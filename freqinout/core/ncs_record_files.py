"""Shared FLDigi/SSB record names; safe for detached ownership validation."""
from pathlib import Path

CURRENT_CHECKIN_FILE_NAMES = {
    "TFC": "CheckIns_TFC.txt",
    "QRU": "CheckIns_QRU.txt",
    "LATE": "CheckIns_LATE.txt",
    "ALL": "CheckIns_ALL.txt",
}

ROLE_CHECKIN_FILE_NAMES = {
    "NCS": {
        "TFC": "NCS_CheckIns_TFC.txt",
        "QRU": "NCS_CheckIns_QRU.txt",
        "LATE": "NCS_CheckIns_LATE.txt",
        "ALL": "NCS_CheckIns_ALL.txt",
        "ACK_PENDING": "NCS_ACK_Pending.txt",
        "NEXT_TFC": "NCS_Next_TFC.txt",
        "RELAYS": "NCS_CheckIns_Relays.txt",
    },
    "ANCS": {
        "TFC": "ANCS_CheckIns_TFC.txt",
        "QRU": "ANCS_CheckIns_QRU.txt",
        "LATE": "ANCS_CheckIns_LATE.txt",
        "ALL": "ANCS_CheckIns_ALL.txt",
        "ACK_PENDING": "ANCS_ACK_Pending.txt",
        "NEXT_TFC": "ANCS_Next_TFC.txt",
        "RELAYS": "ANCS_CheckIns_Relays.txt",
    },
}


def generated_checkin_paths(root):
    base = Path(root)
    return [base / name for name in CURRENT_CHECKIN_FILE_NAMES.values()] + [
        base / name for role in ('NCS', 'ANCS') for name in ROLE_CHECKIN_FILE_NAMES[role].values()]
