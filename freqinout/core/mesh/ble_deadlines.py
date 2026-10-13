"""One ordered budget for Linux BLE pairing and Companion verification."""
PAIRING_SEC = 75.0
PAIR_RELEASE_SEC = 5.0
READVERTISE_SEC = 15.0
COMPANION_CONNECT_SEC = 20.0
VERIFY_SEC = 10.0
# Initial connect + pairing + release + availability + reconnect + verify.
ADAPTER_CONNECT_SEC = 150.0
SUPERVISOR_CONNECT_SEC = ADAPTER_CONNECT_SEC + 8.0
