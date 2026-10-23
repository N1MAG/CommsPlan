"""Detached peer evidence reads using the shared bounded latest-request lane."""
from freqinout.core.peer_evidence import load_peer_evidence
from freqinout.gui.detached_request_lane import DetachedRequestLane


def _read_peer(value, cancelled):
    db_path, callsign, local_call = value
    return load_peer_evidence(db_path, callsign, local_call=local_call, cancelled=cancelled)


class PeerEvidenceLane(DetachedRequestLane):
    def __init__(self, parent=None):
        super().__init__(_read_peer, parent, name="peer-evidence-read")

    def request(self, db_path, callsign, *, local_call=""):
        return self.submit((str(db_path), str(callsign), str(local_call)))
