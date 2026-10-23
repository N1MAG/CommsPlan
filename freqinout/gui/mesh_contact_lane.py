"""Exact-contact local preferences/history on the shared detached worker lane."""
from dataclasses import replace
from freqinout.core.mesh.contacts import load_contact_snapshot, set_contact_favorite, ContactSnapshot
from freqinout.gui.detached_request_lane import DetachedRequestLane


def _contact_job(value, cancelled):
    db_path, identity, favorite = value
    if cancelled():
        return None
    if favorite is not None:
        if not set_contact_favorite(db_path, identity, favorite=favorite):
            return ContactSnapshot(identity, error="Favorite could not be saved.", favorite_saved=False)
    if cancelled():
        return None
    try:
        snapshot = load_contact_snapshot(db_path, identity, cancelled=cancelled)
    except Exception:
        if favorite is None:
            raise
        return ContactSnapshot(identity, favorite=bool(favorite), favorite_saved=True,
                               error="Favorite saved; contact history could not be reloaded.")
    return (replace(snapshot, favorite=bool(favorite), favorite_saved=True)
            if favorite is not None else snapshot)


class MeshContactLane(DetachedRequestLane):
    def __init__(self, parent=None):
        super().__init__(_contact_job, parent, name="mesh-contact-read")

    def request(self, db_path, identity, *, favorite=None):
        # Exact immutable source identity; display names and widgets stay out.
        return self.submit((str(db_path), tuple(identity), favorite))
