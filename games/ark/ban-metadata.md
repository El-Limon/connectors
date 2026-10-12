# ARK ban metadata and expiry

ARK's native ban collection stores Steam64 IDs, not reasons or expiry dates. The
native plugin keeps those fields in `ban-metadata.json` in its state directory
(`<ark install>/.takaro/ark-native` by default, `TAKARO_STATE_DIR` to move it,
or `TAKARO_BAN_METADATA_FILE` for this one file). The format is the same
version-1 document the 0.1 sidecar wrote, so an existing file can be copied into
the state directory. The file is written atomically before a ban is applied
natively. A corrupt existing file stops the Takaro connection at startup rather
than managing bans blindly.

`listBans` reads fresh native membership and joins it to the saved metadata and
the last seen player name (`known-players.json`; Takaro renames a player to the
name in a ban row, so the row never carries the raw Steam64 when a name is
known). If native contains an ID without metadata, `listBans` fails closed because
Takaro's ban sync would otherwise overwrite a managed ban's reason and expiry
with blank/permanent values. To migrate a preexisting native ban, obtain its
actual reason and expiry from the authoritative Takaro ban record or operator,
then reissue an authorized Generic `banPlayer` action for that Steam64 with
those values. Verify the new `listBans` result; do not guess an unknown expiry.

The plugin checks timed bans every second on its own thread, independently of
the Takaro connection. At expiry it applies and verifies the native unban; while
the game thread is unavailable the ban stays enforced until reconciliation
succeeds. Takaro's current ban sync has no expiry-deletion job. An expired
managed database row can remain visible, and a stale global sync request is
rejected before it can re-ban the player. Remove stale managed rows through
Takaro's ban controls.
