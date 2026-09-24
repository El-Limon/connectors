# Console output capture, ShooterGameServer 21241282

This is an offline binding analysis for the exact executable SHA-256
`7e7ded49e0c658e74199801d79630bd33da407d3e468e039837bf61a9de7c520`.
It is not live proof that `GEngine::Exec` handles `ListPlayers` on a dedicated
server.

`0x27779C0` constructs a stack output device at `0x2777A19` by calling
`0xD2FCF0` with an empty UTF-32 literal. The constructor clears the `FString`
at object offset `+0x10`, clears flags at `+0x08/+0x09`, and installs base
vtable `0x4019720`. Its six virtual entries include `Serialize` at slot
`+0x10` → `0xD2FDD0`; that function appends UTF-32 text into the `FString` at
`+0x10`. The caller later frees the string buffer through `0x1AF8E30`
(`0x2777D8F`).

The same caller changes the vtable to `0x4428820` at `0x2777A27` and stores
an optional context pointer at `+0x20`. Its `Serialize` override `0x260F920`
appends through `0xD2FDD0` and also forwards text to engine logging. The
base vtable therefore provides a simpler capture device without that context.
`GEngine::Exec` at `0x262EEB0` receives the output device in the fourth
SysV argument (`RCX`) and returns a handled boolean. This boolean says nothing
about a command's side effects.

The separate `engine_exec_capture.hpp` helper copies all six base vtable
entries and substitutes only `Serialize` with a no-throw fixed 8192-codepoint
collector. It validates the exact constructor, resolver, free, and Exec
function prologues and is currently restricted to `ListPlayers`. Output
overflow fails closed as `output_truncated`; empty output and unhandled
commands remain distinguishable. No game/runtime call has exercised this
helper yet.

Reproduce the referenced bytes offline:

```sh
docker run --rm --network none \
  -v /path/to/ark-server/ShooterGame/Binaries/Linux:/binark:ro \
  gcc:14 objdump -d -M intel --start-address=0x27779c0 --stop-address=0x2777a35 /binark/ShooterGameServer
docker run --rm --network none \
  -v /path/to/ark-server/ShooterGame/Binaries/Linux:/binark:ro \
  gcc:14 objdump -d -M intel --start-address=0xd2fcf0 --stop-address=0xd2fea0 /binark/ShooterGameServer
docker run --rm --network none \
  -v /path/to/ark-server/ShooterGame/Binaries/Linux:/binark:ro \
  gcc:14 objdump -s --start-address=0x4019710 --stop-address=0x4019750 /binark/ShooterGameServer
```
